"""PostgreSQL contracts for subscription management boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.billing import (
    DEFAULT_PRODUCT_CATALOG,
    CapturedPayment,
    PaymentSettlementService,
    PreCheckoutRequest,
    PurchaseIntentService,
    SubscriptionManagementService,
    SubscriptionRenewalCommand,
    SubscriptionRenewalDisposition,
    SubscriptionRenewalWorker,
    SubscriptionStateError,
    SubscriptionStatus,
)
from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import (
    LegalAcceptance,
    Subscription,
    SubscriptionRenewalCommandRecord,
    User,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _charge_id(label: str) -> str:
    return f"{label}:{uuid4().hex}"


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class TrackedTransactions:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.active = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session, session.begin():
            self.active += 1
            try:
                yield session
            finally:
                self.active -= 1


@dataclass(frozen=True, slots=True)
class SubscriptionEnvironment:
    transactions: TrackedTransactions
    intents: PurchaseIntentService
    settlement: PaymentSettlementService
    management: SubscriptionManagementService
    clock: MutableClock


@pytest_asyncio.fixture
async def subscription_env(db_engine: AsyncEngine) -> SubscriptionEnvironment:
    transactions = TrackedTransactions(db_engine)
    clock = MutableClock(NOW)
    settlement = PaymentSettlementService(transactions, clock=clock)
    return SubscriptionEnvironment(
        transactions=transactions,
        intents=PurchaseIntentService(
            transactions,
            clock=clock,
            token_factory=lambda: f"subscription-{uuid4().hex}",
        ),
        settlement=settlement,
        management=SubscriptionManagementService(
            transactions,
            clock=clock,
        ),
        clock=clock,
    )


async def _create_user(env: SubscriptionEnvironment) -> tuple[UUID, int]:
    telegram_id = 20_000_000 + uuid4().int % 8_000_000_000
    async with env.transactions() as session:
        user = User(
            telegram_id=telegram_id,
            is_bot=False,
            first_name="Subscription",
        )
        session.add(user)
        await session.flush()
        session.add(
            LegalAcceptance(
                user_id=user.id,
                document="terms",
                version=TERMS_ACCEPTANCE_VERSION,
                source="terms_command",
                accepted_at=NOW,
            )
        )
        return user.id, telegram_id


async def _start_subscription(
    env: SubscriptionEnvironment,
    user_id: UUID,
    telegram_id: int,
    *,
    charge_id: str,
    period_end: datetime,
) -> str:
    handle = await env.intents.create_subscription_intent(payer_user_id=user_id)
    decision = await env.intents.validate_pre_checkout(
        PreCheckoutRequest(
            handle.invoice_payload,
            telegram_id,
            "XTR",
            handle.stars,
        )
    )
    assert decision.approved
    result = await env.settlement.fulfill(
        CapturedPayment(
            invoice_payload=handle.invoice_payload,
            telegram_charge_id=charge_id,
            provider_charge_id=f"provider:{charge_id}",
            payer_telegram_id=telegram_id,
            currency="XTR",
            total_amount=handle.stars,
            is_recurring=True,
            is_first_recurring=True,
            subscription_expiration_at=period_end,
        )
    )
    assert result.subscription_id is not None
    return handle.invoice_payload


async def _renew_subscription(
    env: SubscriptionEnvironment,
    invoice_payload: str,
    telegram_id: int,
    *,
    charge_id: str,
    period_end: datetime,
) -> None:
    plan = DEFAULT_PRODUCT_CATALOG.subscription_plan
    result = await env.settlement.fulfill(
        CapturedPayment(
            invoice_payload=invoice_payload,
            telegram_charge_id=charge_id,
            provider_charge_id=f"provider:{charge_id}",
            payer_telegram_id=telegram_id,
            currency="XTR",
            total_amount=plan.stars,
            is_recurring=True,
            subscription_expiration_at=period_end,
        )
    )
    assert result.subscription_id is not None


async def test_snapshot_uses_first_charge_across_recurring_cycles(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    first_end = NOW + timedelta(days=30)
    first_charge = _charge_id("stable-first-charge")
    payload = await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=first_charge,
        period_end=first_end,
    )
    second_end = first_end + timedelta(days=30)
    await _renew_subscription(
        subscription_env,
        payload,
        telegram_id,
        charge_id=_charge_id("renewal-charge"),
        period_end=second_end,
    )

    snapshot = await subscription_env.management.get_snapshot(user_id)

    assert snapshot.user_id == user_id
    assert snapshot.payer_telegram_id == telegram_id
    assert snapshot.status is SubscriptionStatus.ACTIVE
    assert snapshot.renewal_enabled
    assert snapshot.current_period_end == second_end
    assert snapshot.telegram_payment_charge_id == first_charge


async def test_snapshot_uses_latest_first_charge_after_resubscription(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    first_end = NOW + timedelta(days=30)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("former-subscription-charge"),
        period_end=first_end,
    )
    subscription_env.clock.now = first_end
    second_end = first_end + timedelta(days=30)
    current_charge = _charge_id("current-subscription-charge")
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=current_charge,
        period_end=second_end,
    )

    snapshot = await subscription_env.management.get_snapshot(user_id)

    assert snapshot.current_period_end == second_end
    assert snapshot.telegram_payment_charge_id == current_charge


async def test_renewal_control_has_no_transaction_across_provider_io(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    period_end = NOW + timedelta(days=30)
    charge_id = _charge_id("control-charge")
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=charge_id,
        period_end=period_end,
    )
    commands: list[SubscriptionRenewalCommand] = []

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            assert subscription_env.transactions.active == 0
            commands.append(command)

    result = await subscription_env.management.set_renewal(
        user_id,
        enabled=False,
        provider=Provider(),
    )

    assert commands == [
        SubscriptionRenewalCommand(
            payer_telegram_id=telegram_id,
            telegram_payment_charge_id=charge_id,
            enabled=False,
        )
    ]
    assert not result.renewal_enabled
    assert result.disposition is SubscriptionRenewalDisposition.APPLIED
    assert result.current_period_end == period_end
    async with subscription_env.transactions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        assert subscription is not None
        assert subscription.status == "canceled"
        assert not subscription.renewal_enabled


async def test_provider_failure_does_not_change_local_renewal_state(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("failing-control-charge"),
        period_end=NOW + timedelta(days=30),
    )

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            assert subscription_env.transactions.active == 0
            raise ConnectionError("provider unavailable")

    result = await subscription_env.management.set_renewal(
        user_id,
        enabled=False,
        provider=Provider(),
    )

    snapshot = await subscription_env.management.get_snapshot(user_id)
    assert result.disposition is SubscriptionRenewalDisposition.PENDING
    assert snapshot.renewal_enabled
    assert snapshot.status is SubscriptionStatus.ACTIVE
    async with subscription_env.transactions() as session:
        command = await session.scalar(
            select(SubscriptionRenewalCommandRecord).where(
                SubscriptionRenewalCommandRecord.subscription_id
                == snapshot.subscription_id
            )
        )
        assert command is not None
        assert command.status == "pending"
        assert command.attempt_count == 1
        assert command.lease_token is None
    restored = await subscription_env.management.set_renewal(
        user_id,
        enabled=True,
        provider=Provider(),
    )
    assert restored.disposition is SubscriptionRenewalDisposition.UNCHANGED


async def test_replayed_renewal_state_skips_provider_io(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("idempotent-control-charge"),
        period_end=NOW + timedelta(days=30),
    )
    provider_called = False

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            nonlocal provider_called
            provider_called = True

    result = await subscription_env.management.set_renewal(
        user_id,
        enabled=True,
        provider=Provider(),
    )

    assert not provider_called
    assert not result.changed
    assert result.disposition is SubscriptionRenewalDisposition.UNCHANGED
    assert result.renewal_enabled


async def test_restart_replays_provider_success_before_local_commit(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    charge_id = _charge_id("ambiguous-success-charge")
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=charge_id,
        period_end=NOW + timedelta(days=30),
    )
    commands: list[SubscriptionRenewalCommand] = []

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            commands.append(command)

    with (
        patch.object(
            subscription_env.management,
            "_apply_claim",
            side_effect=RuntimeError("process stopped after provider success"),
        ),
        pytest.raises(RuntimeError, match="process stopped"),
    ):
        await subscription_env.management.set_renewal(
            user_id,
            enabled=False,
            provider=Provider(),
        )

    snapshot = await subscription_env.management.get_snapshot(user_id)
    assert snapshot.renewal_enabled
    async with subscription_env.transactions() as session:
        command = await session.scalar(
            select(SubscriptionRenewalCommandRecord).where(
                SubscriptionRenewalCommandRecord.subscription_id
                == snapshot.subscription_id
            )
        )
        assert command is not None
        assert command.status == "processing"
        assert command.attempt_count == 1

    subscription_env.clock.now += timedelta(minutes=6)
    restarted = SubscriptionManagementService(
        subscription_env.transactions,
        clock=subscription_env.clock,
    )
    report = await SubscriptionRenewalWorker(
        restarted,
        Provider(),
        batch_size=1,
    ).sweep()

    assert report.claimed_count == 1
    assert report.applied_count == 1
    assert len(commands) == 2
    assert commands[0] == commands[1]
    snapshot = await restarted.get_snapshot(user_id)
    assert not snapshot.renewal_enabled
    assert snapshot.status is SubscriptionStatus.CANCELED
    async with subscription_env.transactions() as session:
        command = await session.scalar(
            select(SubscriptionRenewalCommandRecord).where(
                SubscriptionRenewalCommandRecord.subscription_id
                == snapshot.subscription_id
            )
        )
        assert command is not None
        assert command.status == "applied"
        assert command.attempt_count == 2
        assert command.completed_at is not None


async def test_restart_retries_a_definite_provider_failure(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("retry-charge"),
        period_end=NOW + timedelta(days=30),
    )

    class FailingProvider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            raise ConnectionError("provider unavailable")

    first = await subscription_env.management.set_renewal(
        user_id,
        enabled=False,
        provider=FailingProvider(),
    )
    assert first.disposition is SubscriptionRenewalDisposition.PENDING

    subscription_env.clock.now += timedelta(seconds=30)
    commands: list[SubscriptionRenewalCommand] = []

    class RecoveredProvider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            commands.append(command)

    restarted = SubscriptionManagementService(
        subscription_env.transactions,
        clock=subscription_env.clock,
    )
    report = await SubscriptionRenewalWorker(restarted, RecoveredProvider()).sweep()

    assert report.applied_count == 1
    assert len(commands) == 1
    assert not (await restarted.get_snapshot(user_id)).renewal_enabled


async def test_opposite_request_supersedes_unsubmitted_retry(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("superseded-charge"),
        period_end=NOW + timedelta(days=30),
    )

    class FailingProvider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            raise ConnectionError("provider unavailable")

    pending = await subscription_env.management.set_renewal(
        user_id,
        enabled=False,
        provider=FailingProvider(),
    )
    assert pending.disposition is SubscriptionRenewalDisposition.PENDING

    provider_called = False

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            nonlocal provider_called
            provider_called = True

    restored = await subscription_env.management.set_renewal(
        user_id,
        enabled=True,
        provider=Provider(),
    )

    assert restored.disposition is SubscriptionRenewalDisposition.UNCHANGED
    assert not provider_called
    async with subscription_env.transactions() as session:
        commands = list(
            await session.scalars(
                select(SubscriptionRenewalCommandRecord).where(
                    SubscriptionRenewalCommandRecord.subscription_id
                    == restored.subscription_id
                )
            )
        )
        assert len(commands) == 1
        assert commands[0].status == "superseded"
        assert commands[0].completed_at is not None


async def test_inflight_command_retargets_to_the_latest_absolute_state(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    charge_id = _charge_id("retargeted-charge")
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=charge_id,
        period_end=NOW + timedelta(days=30),
    )

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            return None

    canceled = await subscription_env.management.set_renewal(
        user_id,
        enabled=False,
        provider=Provider(),
    )
    assert not canceled.renewal_enabled

    command_id, immediate = await subscription_env.management._prepare_command(
        user_id,
        True,
    )
    assert command_id is not None
    assert immediate is None
    stale_claim, _ = await subscription_env.management._claim_one(command_id)
    assert stale_claim is not None

    latest = await subscription_env.management.set_renewal(
        user_id,
        enabled=False,
        provider=Provider(),
    )
    assert latest.disposition is SubscriptionRenewalDisposition.PENDING
    provider_commands: list[SubscriptionRenewalCommand] = []

    class RecordingProvider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            provider_commands.append(command)

    stale_result = await subscription_env.management.process_claim(
        stale_claim,
        RecordingProvider(),
    )
    assert stale_result.disposition is SubscriptionRenewalDisposition.PENDING
    latest_claim, _ = await subscription_env.management._claim_one(command_id)
    assert latest_claim is not None
    final = await subscription_env.management.process_claim(
        latest_claim,
        RecordingProvider(),
    )

    assert not final.renewal_enabled
    assert provider_commands == [
        SubscriptionRenewalCommand(telegram_id, charge_id, True),
        SubscriptionRenewalCommand(telegram_id, charge_id, False),
    ]
    async with subscription_env.transactions() as session:
        command = await session.get(SubscriptionRenewalCommandRecord, command_id)
        assert command is not None
        assert command.status == "applied"
        assert not command.desired_enabled


async def test_exhausted_provider_command_requires_attention(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("attention-charge"),
        period_end=NOW + timedelta(days=30),
    )
    service = SubscriptionManagementService(
        subscription_env.transactions,
        clock=subscription_env.clock,
        max_attempts=1,
    )

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            raise ConnectionError("provider unavailable")

    result = await service.set_renewal(
        user_id,
        enabled=False,
        provider=Provider(),
    )

    assert result.disposition is SubscriptionRenewalDisposition.ATTENTION
    async with subscription_env.transactions() as session:
        command = await session.scalar(
            select(SubscriptionRenewalCommandRecord).where(
                SubscriptionRenewalCommandRecord.subscription_id
                == result.subscription_id
            )
        )
        assert command is not None
        assert command.status == "attention"
        assert command.last_failure_code == "provider_call_failed"


async def test_expired_cycle_is_rejected_before_provider_io(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    period_end = NOW + timedelta(days=30)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id=_charge_id("expired-control-charge"),
        period_end=period_end,
    )
    subscription_env.clock.now = period_end
    provider_called = False

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            nonlocal provider_called
            provider_called = True

    with pytest.raises(SubscriptionStateError, match="expired"):
        await subscription_env.management.set_renewal(
            user_id,
            enabled=False,
            provider=Provider(),
        )

    assert not provider_called


async def test_snapshot_rejects_missing_subscription(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, _ = await _create_user(subscription_env)

    with pytest.raises(SubscriptionStateError, match="no subscription"):
        await subscription_env.management.get_snapshot(user_id)


async def test_renewal_service_rejects_non_boolean_state_before_database_io(
    subscription_env: SubscriptionEnvironment,
) -> None:
    with pytest.raises(TypeError, match="enabled must be a bool"):
        await subscription_env.management.set_renewal(
            UUID(int=1),
            enabled=1,  # type: ignore[arg-type]
            provider=object(),  # type: ignore[arg-type]
        )


def test_renewal_service_rejects_non_integer_attempt_limit() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        SubscriptionManagementService(
            lambda: None,  # type: ignore[arg-type,return-value]
            max_attempts="8",  # type: ignore[arg-type]
        )
