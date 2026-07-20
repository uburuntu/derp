"""PostgreSQL contracts for durable Stars commerce and subscription cycles."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.billing import (
    DEFAULT_PRODUCT_CATALOG,
    ActiveSubscriptionError,
    CapturedPayment,
    FulfillmentState,
    PaymentConflictError,
    PaymentSettlementService,
    PreCheckoutRejection,
    PreCheckoutRequest,
    PurchaseIntentService,
    PurchaseTarget,
    UnknownProductError,
)
from derp.billing.payloads import hash_invoice_payload
from derp.billing.types import RefundedPaymentCommand
from derp.models import (
    Chat,
    PaymentReceipt,
    PurchaseIntent,
    Subscription,
    SubscriptionCycle,
    User,
    Wallet,
    WalletLedgerEntry,
    WalletLot,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class CommerceEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    intents: PurchaseIntentService
    settlement: PaymentSettlementService
    clock: MutableClock


@pytest_asyncio.fixture
async def commerce_env(db_engine: AsyncEngine) -> AsyncIterator[CommerceEnvironment]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    counter = itertools.count(1)
    clock = MutableClock(NOW)
    yield CommerceEnvironment(
        transactions=transactions,
        intents=PurchaseIntentService(
            transactions,
            clock=clock,
            token_factory=lambda: f"test-{next(counter)}-{uuid4().hex}",
        ),
        settlement=PaymentSettlementService(transactions, clock=clock),
        clock=clock,
    )


def _telegram_id() -> int:
    return 20_000_000 + uuid4().int % 8_000_000_000


async def _create_user(env: CommerceEnvironment) -> tuple[UUID, int]:
    telegram_id = _telegram_id()
    async with env.transactions() as session:
        user = User(
            telegram_id=telegram_id,
            is_bot=False,
            first_name="Commerce",
        )
        session.add(user)
        await session.flush()
        return user.id, telegram_id


async def _create_chat(env: CommerceEnvironment) -> UUID:
    async with env.transactions() as session:
        chat = Chat(telegram_id=-_telegram_id(), type="supergroup")
        session.add(chat)
        await session.flush()
        return chat.id


async def _prechecked_top_up(
    env: CommerceEnvironment,
    *,
    user_id: UUID,
    telegram_id: int,
    target: PurchaseTarget | None = None,
    product_id: str = "starter",
):
    handle = await env.intents.create_top_up_intent(
        payer_user_id=user_id,
        target=target or PurchaseTarget.user(user_id),
        product_id=product_id,
    )
    decision = await env.intents.validate_pre_checkout(
        PreCheckoutRequest(
            invoice_payload=handle.invoice_payload,
            payer_telegram_id=telegram_id,
            currency="XTR",
            total_amount=handle.stars,
        )
    )
    assert decision.approved
    return handle


def _top_up_payment(handle, telegram_id: int, charge_id: str) -> CapturedPayment:
    return CapturedPayment(
        invoice_payload=handle.invoice_payload,
        telegram_charge_id=charge_id,
        provider_charge_id=f"provider:{charge_id}",
        payer_telegram_id=telegram_id,
        currency="XTR",
        total_amount=handle.stars,
    )


def _refund_command(
    handle,
    charge_id: str,
    **overrides,
) -> RefundedPaymentCommand:
    values = {
        "invoice_payload": handle.invoice_payload,
        "telegram_charge_id": charge_id,
        "provider_charge_id": f"provider:{charge_id}",
        "currency": "XTR",
        "total_amount": handle.stars,
    }
    values.update(overrides)
    return RefundedPaymentCommand(**values)


async def test_intent_is_hashed_and_precheckout_validates_every_payment_field(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await commerce_env.intents.create_top_up_intent(
        payer_user_id=user_id,
        target=PurchaseTarget.user(user_id),
        product_id="starter",
    )

    wrong_payer = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id + 1, "XTR", handle.stars)
    )
    wrong_currency = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "USD", handle.stars)
    )
    wrong_amount = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "XTR", handle.stars + 1)
    )
    approved = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "XTR", handle.stars)
    )

    assert wrong_payer.rejection is PreCheckoutRejection.PAYER_MISMATCH
    assert wrong_currency.rejection is PreCheckoutRejection.CURRENCY_MISMATCH
    assert wrong_amount.rejection is PreCheckoutRejection.AMOUNT_MISMATCH
    assert approved.approved
    async with commerce_env.transactions() as session:
        intent = await session.get(PurchaseIntent, handle.intent_id)
        assert intent is not None
        assert intent.token_hash == hash_invoice_payload(handle.invoice_payload)
        assert handle.invoice_payload not in repr(intent.token_hash)
        assert intent.product_version == handle.product_version
        assert intent.status == "prechecked"


async def test_hidden_debug_product_requires_explicit_intake_and_normal_settlement(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up

    with pytest.raises(UnknownProductError):
        await commerce_env.intents.create_top_up_intent(
            payer_user_id=user_id,
            target=PurchaseTarget.user(user_id),
            product_id=product.id,
        )

    handle = await commerce_env.intents.create_admin_debug_top_up_intent(
        payer_user_id=user_id,
        target=PurchaseTarget.user(user_id),
    )
    decision = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(
            invoice_payload=handle.invoice_payload,
            payer_telegram_id=telegram_id,
            currency="XTR",
            total_amount=1,
        )
    )
    result = await commerce_env.settlement.fulfill(
        _top_up_payment(handle, telegram_id, f"debug-one-star:{uuid4().hex}")
    )

    assert decision.approved
    assert handle.product_id == product.id
    assert handle.stars == 1
    assert handle.credits == product.credits
    assert handle.invoice_payload.startswith("dpi1_")
    assert result.state is FulfillmentState.FULFILLED
    assert result.available_credits == product.credits


async def test_expired_intent_fails_closed(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await commerce_env.intents.create_top_up_intent(
        payer_user_id=user_id,
        target=PurchaseTarget.user(user_id),
        product_id="starter",
    )
    commerce_env.clock.now = handle.expires_at

    decision = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "XTR", handle.stars)
    )

    assert decision.rejection is PreCheckoutRejection.EXPIRED


async def test_retired_product_version_fails_precheckout_closed(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await commerce_env.intents.create_top_up_intent(
        payer_user_id=user_id,
        target=PurchaseTarget.user(user_id),
        product_id="starter",
    )
    async with commerce_env.transactions() as session:
        intent = await session.get(PurchaseIntent, handle.intent_id)
        assert intent is not None
        intent.product_version = "retired"

    decision = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "XTR", handle.stars)
    )

    assert decision.rejection is PreCheckoutRejection.PRODUCT_MISMATCH


async def test_top_up_fulfillment_offsets_debt_and_is_idempotent(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    async with commerce_env.transactions() as session:
        session.add(Wallet(user_id=user_id, debt_credits=30))
    handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    payment = _top_up_payment(handle, telegram_id, "topup-debt")

    first = await commerce_env.settlement.fulfill(payment)
    duplicate = await commerce_env.settlement.fulfill(payment)

    assert first.state is FulfillmentState.FULFILLED and not first.idempotent
    assert first.available_credits == 20
    assert first.debt_offset_credits == 30
    assert duplicate.idempotent
    async with commerce_env.transactions() as session:
        wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        lots = list(
            await session.scalars(
                select(WalletLot).where(WalletLot.wallet_id == wallet.id)
            )
        )
        events = set(
            await session.scalars(
                select(WalletLedgerEntry.event_type).where(
                    WalletLedgerEntry.wallet_id == wallet.id
                )
            )
        )
        assert wallet.debt_credits == 0
        assert len(lots) == 1
        assert lots[0].available_credits == 20
        assert lots[0].debt_offset_credits == 30
        assert events == {"grant", "debt_offset"}


async def test_concurrent_duplicate_payment_creates_one_receipt_and_lot(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    payment = _top_up_payment(handle, telegram_id, "same-charge")

    results = await asyncio.gather(
        commerce_env.settlement.fulfill(payment),
        commerce_env.settlement.fulfill(payment),
    )

    assert sum(result.idempotent for result in results) == 1
    assert all(result.state is FulfillmentState.FULFILLED for result in results)
    async with commerce_env.transactions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PaymentReceipt)
                .where(PaymentReceipt.telegram_charge_id == "same-charge")
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(WalletLot)
                .where(WalletLot.payment_receipt_id == results[0].receipt_id)
            )
            == 1
        )


async def test_charge_id_replay_with_changed_fields_is_rejected(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    payment = _top_up_payment(handle, telegram_id, "conflicting-charge")
    await commerce_env.settlement.fulfill(payment)

    with pytest.raises(PaymentConflictError):
        await commerce_env.settlement.fulfill(
            CapturedPayment(
                invoice_payload=payment.invoice_payload,
                telegram_charge_id=payment.telegram_charge_id,
                provider_charge_id="different-provider",
                payer_telegram_id=telegram_id,
                currency="XTR",
                total_amount=handle.stars,
            )
        )


async def test_chat_top_up_credits_only_the_bound_chat_wallet(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    chat_id = await _create_chat(commerce_env)
    handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
        target=PurchaseTarget.chat(chat_id),
    )

    result = await commerce_env.settlement.fulfill(
        _top_up_payment(handle, telegram_id, "chat-charge")
    )

    async with commerce_env.transactions() as session:
        wallet = await session.get(Wallet, result.wallet_id)
        assert wallet is not None
        assert wallet.chat_id == chat_id
        assert wallet.user_id is None


async def test_subscription_renews_without_rollover_and_cancellation_keeps_cycle(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await commerce_env.intents.create_subscription_intent(
        payer_user_id=user_id
    )
    plan = DEFAULT_PRODUCT_CATALOG.subscription_plan
    decision = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "XTR", plan.stars)
    )
    assert decision.approved
    first_end = NOW + timedelta(days=30)
    first = await commerce_env.settlement.fulfill(
        CapturedPayment(
            handle.invoice_payload,
            "subscription-first",
            "provider-subscription-first",
            telegram_id,
            "XTR",
            plan.stars,
            is_recurring=True,
            is_first_recurring=True,
            subscription_expiration_at=first_end,
        )
    )

    canceled = await commerce_env.settlement.set_subscription_renewal(
        user_id,
        enabled=False,
    )
    assert not canceled.renewal_enabled
    assert canceled.current_period_end == first_end
    with pytest.raises(ActiveSubscriptionError):
        await commerce_env.intents.create_subscription_intent(payer_user_id=user_id)
    reenabled = await commerce_env.settlement.set_subscription_renewal(
        user_id,
        enabled=True,
    )
    assert reenabled.renewal_enabled

    second_end = first_end + timedelta(days=30)
    renewal = CapturedPayment(
        handle.invoice_payload,
        "subscription-renewal",
        "provider-subscription-renewal",
        telegram_id,
        "XTR",
        plan.stars,
        is_recurring=True,
        subscription_expiration_at=second_end,
    )
    second = await commerce_env.settlement.fulfill(renewal)
    duplicate_cycle = await commerce_env.settlement.fulfill(
        CapturedPayment(
            handle.invoice_payload,
            "subscription-duplicate-cycle",
            "provider-subscription-duplicate-cycle",
            telegram_id,
            "XTR",
            plan.stars,
            is_recurring=True,
            subscription_expiration_at=second_end,
        )
    )

    assert first.state is second.state is FulfillmentState.FULFILLED
    assert duplicate_cycle.state is FulfillmentState.NEEDS_REVIEW
    assert duplicate_cycle.review_reason == "duplicate_subscription_cycle"
    async with commerce_env.transactions() as session:
        cycles = list(
            await session.scalars(
                select(SubscriptionCycle).order_by(SubscriptionCycle.period_end)
            )
        )
        first_lot = await session.get(WalletLot, first.wallet_lot_id)
        second_lot = await session.get(WalletLot, second.wallet_lot_id)
        subscription = await session.get(Subscription, second.subscription_id)
        assert len(cycles) == 2
        assert [cycle.status for cycle in cycles] == ["expired", "active"]
        assert first_lot.available_credits == 0
        assert first_lot.expired_credits == plan.allowance_credits
        assert second_lot.available_credits == plan.allowance_credits
        assert subscription.current_period_end == second_end


async def test_subscription_clawback_reconciles_its_exact_cycle_source(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await commerce_env.intents.create_subscription_intent(
        payer_user_id=user_id
    )
    plan = DEFAULT_PRODUCT_CATALOG.subscription_plan
    decision = await commerce_env.intents.validate_pre_checkout(
        PreCheckoutRequest(handle.invoice_payload, telegram_id, "XTR", plan.stars)
    )
    assert decision.approved
    result = await commerce_env.settlement.fulfill(
        CapturedPayment(
            handle.invoice_payload,
            "clawed-subscription",
            "provider-clawed-subscription",
            telegram_id,
            "XTR",
            plan.stars,
            is_recurring=True,
            is_first_recurring=True,
            subscription_expiration_at=NOW + timedelta(days=30),
        )
    )
    consumed = 75
    async with commerce_env.transactions() as session:
        lot = await session.get(WalletLot, result.wallet_lot_id, with_for_update=True)
        assert lot is not None
        lot.available_credits -= consumed
        lot.consumed_credits += consumed

    clawback = await commerce_env.settlement.clawback("clawed-subscription")
    duplicate = await commerce_env.settlement.clawback("clawed-subscription")

    assert clawback.removed_available_credits == plan.allowance_credits - consumed
    assert clawback.debt_created_credits == consumed
    assert duplicate.idempotent
    async with commerce_env.transactions() as session:
        lot = await session.get(WalletLot, result.wallet_lot_id)
        cycle = await session.get(SubscriptionCycle, result.subscription_cycle_id)
        receipt = await session.get(PaymentReceipt, result.receipt_id)
        wallet = await session.get(Wallet, result.wallet_id)
        assert lot is not None
        assert cycle is not None
        assert receipt is not None
        assert wallet is not None
        assert lot.available_credits == 0
        assert lot.clawed_back_credits == plan.allowance_credits - consumed
        assert cycle.status == "clawed_back"
        assert receipt.status == "clawed_back"
        assert wallet.debt_credits == consumed


async def test_typed_refund_accepts_missing_optional_provider_id_and_is_idempotent(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    charge_id = "typed-refund-without-provider"
    fulfilled = await commerce_env.settlement.fulfill(
        _top_up_payment(handle, telegram_id, charge_id)
    )
    refund = _refund_command(handle, charge_id, provider_charge_id=None)

    clawback = await commerce_env.settlement.clawback(refund)
    duplicate = await commerce_env.settlement.clawback(refund)

    assert clawback.wallet_id == fulfilled.wallet_id
    assert clawback.removed_available_credits == handle.credits
    assert clawback.debt_created_credits == 0
    assert not clawback.idempotent
    assert duplicate.idempotent


@pytest.mark.parametrize(
    ("mismatched_field", "mismatched_value"),
    [
        pytest.param("invoice_payload", "dpi1_different", id="payload"),
        pytest.param("provider_charge_id", "provider:different", id="provider"),
        pytest.param("currency", "USD", id="currency"),
        pytest.param("total_amount", 999_999, id="amount"),
    ],
)
async def test_refund_mismatch_does_not_mutate_receipt_or_wallet(
    commerce_env: CommerceEnvironment,
    mismatched_field: str,
    mismatched_value: str | int,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    charge_id = f"refund-mismatch-{mismatched_field}"
    fulfilled = await commerce_env.settlement.fulfill(
        _top_up_payment(handle, telegram_id, charge_id)
    )
    refund = _refund_command(
        handle,
        charge_id,
        **{mismatched_field: mismatched_value},
    )

    with pytest.raises(PaymentConflictError):
        await commerce_env.settlement.clawback(refund)

    async with commerce_env.transactions() as session:
        receipt = await session.get(PaymentReceipt, fulfilled.receipt_id)
        lot = await session.get(WalletLot, fulfilled.wallet_lot_id)
        wallet = await session.get(Wallet, fulfilled.wallet_id)
        event_types = list(
            await session.scalars(
                select(WalletLedgerEntry.event_type).where(
                    WalletLedgerEntry.payment_receipt_id == fulfilled.receipt_id
                )
            )
        )
        assert receipt is not None and receipt.status == "fulfilled"
        assert receipt.clawed_back_at is None
        assert lot is not None and lot.available_credits == handle.credits
        assert lot.clawed_back_credits == 0
        assert wallet is not None and wallet.debt_credits == 0
        assert event_types == ["grant"]


async def test_clawback_is_source_specific_and_later_grant_reconciles_debt(
    commerce_env: CommerceEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(commerce_env)
    first_handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    second_handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    first = await commerce_env.settlement.fulfill(
        _top_up_payment(first_handle, telegram_id, "clawback-source")
    )
    second = await commerce_env.settlement.fulfill(
        _top_up_payment(second_handle, telegram_id, "unrelated-source")
    )
    async with commerce_env.transactions() as session:
        source = await session.get(WalletLot, first.wallet_lot_id, with_for_update=True)
        source.available_credits -= 20
        source.reserved_credits = 10
        source.consumed_credits = 10

    clawback = await commerce_env.settlement.clawback("clawback-source")
    duplicate = await commerce_env.settlement.clawback("clawback-source")

    assert clawback.removed_available_credits == 30
    assert clawback.debt_created_credits == 20
    assert duplicate.idempotent
    async with commerce_env.transactions() as session:
        source = await session.get(WalletLot, first.wallet_lot_id)
        unrelated = await session.get(WalletLot, second.wallet_lot_id)
        wallet = await session.get(Wallet, first.wallet_id)
        assert source.available_credits == 0
        assert source.clawed_back_credits == 30
        assert unrelated.available_credits == second_handle.credits
        assert wallet.debt_credits == 20

    third_handle = await _prechecked_top_up(
        commerce_env,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    third = await commerce_env.settlement.fulfill(
        _top_up_payment(third_handle, telegram_id, "debt-repayment")
    )
    assert third.debt_offset_credits == 20
    assert third.available_credits == third_handle.credits - 20
    async with commerce_env.transactions() as session:
        wallet = await session.get(Wallet, third.wallet_id)
        assert wallet.debt_credits == 0


async def test_unmatched_captured_payment_is_durable_needs_review(
    commerce_env: CommerceEnvironment,
) -> None:
    _, telegram_id = await _create_user(commerce_env)

    result = await commerce_env.settlement.fulfill(
        CapturedPayment(
            "dpi1_unknown",
            "unknown-intent-charge",
            "provider-unknown",
            telegram_id,
            "XTR",
            50,
        )
    )

    assert result.state is FulfillmentState.NEEDS_REVIEW
    assert result.review_reason == "unknown_intent"
    async with commerce_env.transactions() as session:
        receipt = await session.get(PaymentReceipt, result.receipt_id)
        assert receipt.status == "needs_review"
        assert receipt.purchase_intent_id is None
