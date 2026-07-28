"""PostgreSQL crash-boundary contracts for the durable Stars update inbox."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from derp.billing import (
    CapturedPayment,
    FulfillmentState,
    PaymentReplyDisposition,
    PaymentSettlementService,
    PaymentSettlementState,
    PaymentUpdateConflictError,
    PaymentUpdateDisposition,
    PaymentUpdateEnvelope,
    PaymentUpdateInboxService,
    PaymentUpdateKind,
    PaymentUpdateReplayWorker,
    PreCheckoutRequest,
    PurchaseIntentService,
    PurchaseTarget,
)
from derp.billing.payloads import hash_invoice_payload
from derp.models import PaymentReceipt, PaymentUpdateInbox, User, WalletLot
from derp.support import TermsAcceptanceService

pytestmark = pytest.mark.database
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class InboxEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    clock: MutableClock
    intents: PurchaseIntentService
    settlement: PaymentSettlementService
    inbox: PaymentUpdateInboxService


@pytest_asyncio.fixture
async def inbox_env(db_session: AsyncSession) -> InboxEnvironment:
    sessions = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with sessions() as session, session.begin():
            yield session

    clock = MutableClock(NOW)
    settlement = PaymentSettlementService(transactions, clock=clock)
    return InboxEnvironment(
        transactions=transactions,
        clock=clock,
        intents=PurchaseIntentService(
            transactions,
            clock=clock,
            token_factory=lambda: f"inbox-{uuid4().hex}",
        ),
        settlement=settlement,
        inbox=PaymentUpdateInboxService(
            transactions,
            settlement,
            clock=clock,
            lease_duration=timedelta(minutes=1),
        ),
    )


async def _prepared_payment(
    env: InboxEnvironment,
    *,
    update_id: int | None = None,
) -> tuple[PaymentUpdateEnvelope, str, UUID]:
    telegram_id = 20_000_000 + uuid4().int % 8_000_000_000
    async with env.transactions() as session:
        user = User(
            telegram_id=telegram_id,
            is_bot=False,
            first_name="Inbox",
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    await TermsAcceptanceService(
        env.transactions,
        clock=env.clock,
    ).accept_current(user_id, source="purchase_gate")
    handle = await env.intents.create_top_up_intent(
        payer_user_id=user_id,
        target=PurchaseTarget.user(user_id),
        product_id="starter",
    )
    decision = await env.intents.validate_pre_checkout(
        PreCheckoutRequest(
            invoice_payload=handle.invoice_payload,
            payer_telegram_id=telegram_id,
            currency=handle.currency,
            total_amount=handle.stars,
        )
    )
    assert decision.approved
    charge_id = f"inbox-charge-{uuid4().hex}"
    return (
        PaymentUpdateEnvelope(
            telegram_update_id=update_id or uuid4().int % 2_000_000_000,
            kind=PaymentUpdateKind.SUCCESSFUL,
            payload_token_hash=hash_invoice_payload(handle.invoice_payload),
            telegram_charge_id=charge_id,
            provider_charge_id=f"provider-{uuid4().hex}",
            payer_telegram_id=telegram_id,
            reply_chat_id=telegram_id,
            reply_language="en",
            currency=handle.currency,
            total_amount=handle.stars,
        ),
        handle.invoice_payload,
        user_id,
    )


def _restart(env: InboxEnvironment) -> PaymentUpdateInboxService:
    return PaymentUpdateInboxService(
        env.transactions,
        env.settlement,
        clock=env.clock,
        lease_duration=timedelta(minutes=1),
    )


async def test_persist_is_idempotent_and_never_stores_plaintext_payload(
    inbox_env: InboxEnvironment,
) -> None:
    envelope, plaintext_payload, _ = await _prepared_payment(inbox_env)

    first = await inbox_env.inbox.persist(envelope)
    second = await inbox_env.inbox.persist(envelope)

    assert first == second
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, first)
        assert row is not None
        assert row.payload_token_hash == hash_invoice_payload(plaintext_payload)
        assert plaintext_payload not in repr(row.__dict__)

    changed = replace(envelope, total_amount=envelope.total_amount + 1)
    with pytest.raises(PaymentUpdateConflictError):
        await inbox_env.inbox.persist(changed)


async def test_restart_replays_update_committed_before_handler(
    inbox_env: InboxEnvironment,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    inbox_id = await inbox_env.inbox.persist(envelope)
    notifier = SimpleNamespace(
        notify=AsyncMock(return_value=PaymentReplyDisposition.SENT)
    )

    report = await PaymentUpdateReplayWorker(
        _restart(inbox_env),
        notifier,
    ).sweep()

    assert report.claimed_count == 1
    assert report.settled_count == 1
    notifier.notify.assert_awaited_once()
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.status == "completed"
        assert row.reply_status == "sent"


async def test_crash_after_settlement_commit_replays_idempotently(
    inbox_env: InboxEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    inbox_id = await inbox_env.inbox.persist(envelope)
    original_mark_settled = inbox_env.inbox._mark_settled
    monkeypatch.setattr(
        inbox_env.inbox,
        "_mark_settled",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await inbox_env.inbox.reconcile(inbox_id)

    monkeypatch.setattr(inbox_env.inbox, "_mark_settled", original_mark_settled)
    inbox_env.clock.now += timedelta(minutes=2)
    outcome = await _restart(inbox_env).reconcile(inbox_id)

    assert outcome.disposition is PaymentUpdateDisposition.SETTLED
    assert outcome.fulfillment is not None
    assert outcome.fulfillment.idempotent
    await _restart(inbox_env).finish_notification(
        outcome,
        PaymentReplyDisposition.SENT,
    )
    async with inbox_env.transactions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PaymentReceipt)
                .where(PaymentReceipt.telegram_charge_id == envelope.telegram_charge_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(WalletLot)
                .join(
                    PaymentReceipt,
                    WalletLot.payment_receipt_id == PaymentReceipt.id,
                )
                .where(PaymentReceipt.telegram_charge_id == envelope.telegram_charge_id)
            )
            == 1
        )


async def test_crash_before_reply_resumes_without_repeating_settlement(
    inbox_env: InboxEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    inbox_id = await inbox_env.inbox.persist(envelope)
    fulfillment = AsyncMock(wraps=inbox_env.settlement.fulfill)
    monkeypatch.setattr(inbox_env.settlement, "fulfill", fulfillment)

    first = await inbox_env.inbox.reconcile(inbox_id)
    assert first.disposition is PaymentUpdateDisposition.SETTLED
    assert fulfillment.await_count == 1

    inbox_env.clock.now += timedelta(minutes=2)
    recovered = await _restart(inbox_env).reconcile(inbox_id)

    assert recovered.disposition is PaymentUpdateDisposition.SETTLED
    assert recovered.fulfillment is None
    assert fulfillment.await_count == 1
    await _restart(inbox_env).finish_notification(
        recovered,
        PaymentReplyDisposition.SENT,
    )


async def test_success_replayed_after_refund_retains_clawed_back_notice_state(
    inbox_env: InboxEnvironment,
) -> None:
    envelope, plaintext_payload, _ = await _prepared_payment(inbox_env)
    await inbox_env.settlement.fulfill(
        CapturedPayment(
            invoice_payload=plaintext_payload,
            telegram_charge_id=envelope.telegram_charge_id,
            provider_charge_id=envelope.provider_charge_id or "",
            payer_telegram_id=envelope.payer_telegram_id or 0,
            currency=envelope.currency,
            total_amount=envelope.total_amount,
        )
    )
    await inbox_env.settlement.clawback(envelope.telegram_charge_id)
    inbox_id = await inbox_env.inbox.persist(envelope)

    first = await inbox_env.inbox.reconcile(inbox_id)

    assert first.fulfillment is not None
    assert first.fulfillment.state is FulfillmentState.CLAWED_BACK
    assert first.settlement_state is PaymentSettlementState.CLAWED_BACK

    inbox_env.clock.now += timedelta(minutes=2)
    recovered = await _restart(inbox_env).reconcile(inbox_id)

    assert recovered.fulfillment is None
    assert recovered.settlement_state is PaymentSettlementState.CLAWED_BACK
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.settlement_state == PaymentSettlementState.CLAWED_BACK.value


async def test_refund_replay_does_not_repeat_clawback(
    inbox_env: InboxEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, plaintext_payload, _ = await _prepared_payment(inbox_env)
    await inbox_env.settlement.fulfill(
        CapturedPayment(
            invoice_payload=plaintext_payload,
            telegram_charge_id=envelope.telegram_charge_id,
            provider_charge_id=envelope.provider_charge_id or "",
            payer_telegram_id=envelope.payer_telegram_id or 0,
            currency=envelope.currency,
            total_amount=envelope.total_amount,
        )
    )
    refund = PaymentUpdateEnvelope(
        telegram_update_id=envelope.telegram_update_id + 1,
        kind=PaymentUpdateKind.REFUNDED,
        payload_token_hash=envelope.payload_token_hash,
        telegram_charge_id=envelope.telegram_charge_id,
        provider_charge_id=envelope.provider_charge_id,
        payer_telegram_id=None,
        reply_chat_id=envelope.reply_chat_id,
        reply_language="en",
        currency="XTR",
        total_amount=envelope.total_amount,
    )
    inbox_id = await inbox_env.inbox.persist(refund)
    clawback = AsyncMock(wraps=inbox_env.settlement.clawback)
    monkeypatch.setattr(inbox_env.settlement, "clawback", clawback)

    first = await inbox_env.inbox.reconcile(inbox_id)
    assert first.disposition is PaymentUpdateDisposition.SETTLED
    assert clawback.await_count == 1

    inbox_env.clock.now += timedelta(minutes=2)
    recovered = await _restart(inbox_env).reconcile(inbox_id)

    assert recovered.disposition is PaymentUpdateDisposition.SETTLED
    assert clawback.await_count == 1
    await _restart(inbox_env).finish_notification(
        recovered,
        PaymentReplyDisposition.SENT,
    )
    async with inbox_env.transactions() as session:
        receipt = await session.scalar(
            select(PaymentReceipt).where(
                PaymentReceipt.telegram_charge_id == envelope.telegram_charge_id
            )
        )
        assert receipt is not None
        assert receipt.status == "clawed_back"


async def test_transient_failure_retries_then_requires_attention(
    inbox_env: InboxEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    service = PaymentUpdateInboxService(
        inbox_env.transactions,
        inbox_env.settlement,
        clock=inbox_env.clock,
        lease_duration=timedelta(minutes=1),
        retry_interval=timedelta(seconds=10),
        max_attempts=2,
    )
    inbox_id = await service.persist(envelope)
    fulfillment = AsyncMock(side_effect=OSError("settlement unavailable"))
    monkeypatch.setattr(inbox_env.settlement, "fulfill", fulfillment)

    first = await service.reconcile(inbox_id)

    assert first.disposition is PaymentUpdateDisposition.RETRY_SCHEDULED
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.status == "pending"
        assert row.attempt_count == 1
        assert row.next_attempt_at == NOW + timedelta(seconds=10)

    inbox_env.clock.now += timedelta(seconds=11)
    exhausted = await service.reconcile(inbox_id)

    assert exhausted.disposition is PaymentUpdateDisposition.ATTENTION
    assert exhausted.attention_reason == "settlement_attempts_exhausted"
    assert fulfillment.await_count == 2
    await service.finish_notification(
        exhausted,
        PaymentReplyDisposition.SKIPPED,
    )
    inbox_env.clock.now += timedelta(minutes=2)
    assert await service.claim_due() == ()


async def test_notification_crash_retries_reply_without_repeating_settlement(
    inbox_env: InboxEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    inbox_id = await inbox_env.inbox.persist(envelope)
    fulfillment = AsyncMock(wraps=inbox_env.settlement.fulfill)
    monkeypatch.setattr(inbox_env.settlement, "fulfill", fulfillment)
    crashing_notifier = SimpleNamespace(
        notify=AsyncMock(side_effect=OSError("Telegram unavailable"))
    )

    interrupted = await PaymentUpdateReplayWorker(
        inbox_env.inbox,
        crashing_notifier,
    ).sweep()

    assert interrupted.claimed_count == 1
    assert interrupted.settled_count == 1
    assert interrupted.reply_sent_count == 0
    assert fulfillment.await_count == 1
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.status == "processing"
        assert row.settled_at == NOW
        assert row.reply_status == "pending"

    inbox_env.clock.now += timedelta(minutes=2)
    recovered_notifier = SimpleNamespace(
        notify=AsyncMock(return_value=PaymentReplyDisposition.SENT)
    )
    recovered = await PaymentUpdateReplayWorker(
        _restart(inbox_env),
        recovered_notifier,
    ).sweep()

    assert recovered.claimed_count == 1
    assert recovered.settled_count == 1
    assert recovered.reply_sent_count == 1
    assert fulfillment.await_count == 1
    recovered_notifier.notify.assert_awaited_once()


async def test_failed_reply_is_retried_without_repeating_settlement(
    inbox_env: InboxEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    service = PaymentUpdateInboxService(
        inbox_env.transactions,
        inbox_env.settlement,
        clock=inbox_env.clock,
        lease_duration=timedelta(minutes=1),
        retry_interval=timedelta(seconds=10),
        max_reply_attempts=2,
    )
    inbox_id = await service.persist(envelope)
    fulfillment = AsyncMock(wraps=inbox_env.settlement.fulfill)
    monkeypatch.setattr(inbox_env.settlement, "fulfill", fulfillment)

    first = await service.reconcile(inbox_id)
    await service.finish_notification(first, PaymentReplyDisposition.FAILED)

    assert fulfillment.await_count == 1
    assert await service.claim_due() == ()
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.status == "pending"
        assert row.settled_at == NOW
        assert row.reply_status == "failed"
        assert row.reply_attempt_count == 1
        assert row.next_attempt_at == NOW + timedelta(seconds=10)

    inbox_env.clock.now += timedelta(seconds=11)
    recovered = await service.reconcile(inbox_id)

    assert recovered.disposition is PaymentUpdateDisposition.SETTLED
    assert recovered.fulfillment is None
    assert fulfillment.await_count == 1
    await service.finish_notification(recovered, PaymentReplyDisposition.SENT)
    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.status == "completed"
        assert row.reply_status == "sent"
        assert row.reply_attempt_count == 2
        assert row.replied_at == inbox_env.clock.now


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (PaymentReplyDisposition.FAILED, "reply_attempts_exhausted"),
        (PaymentReplyDisposition.SKIPPED, "reply_destination_unavailable"),
    ],
)
async def test_unrecoverable_reply_enters_operator_attention(
    inbox_env: InboxEnvironment,
    reply: PaymentReplyDisposition,
    reason: str,
) -> None:
    envelope, _, _ = await _prepared_payment(inbox_env)
    service = PaymentUpdateInboxService(
        inbox_env.transactions,
        inbox_env.settlement,
        clock=inbox_env.clock,
        max_reply_attempts=1,
    )
    inbox_id = await service.persist(envelope)
    outcome = await service.reconcile(inbox_id)

    await service.finish_notification(outcome, reply)

    async with inbox_env.transactions() as session:
        row = await session.get(PaymentUpdateInbox, inbox_id)
        assert row is not None
        assert row.status == "attention"
        assert row.attention_reason == reason
        assert row.last_failure_code == reason
        assert row.reply_status == reply.value
        assert row.reply_attempt_count == 1
    inbox_env.clock.now += timedelta(days=1)
    assert await service.claim_due() == ()
