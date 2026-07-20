"""PostgreSQL contracts for subscription management boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    SubscriptionStateError,
    SubscriptionStatus,
)
from derp.models import Subscription, User

pytestmark = pytest.mark.database
NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


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
    payload = await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id="stable-first-charge",
        period_end=first_end,
    )
    second_end = first_end + timedelta(days=30)
    await _renew_subscription(
        subscription_env,
        payload,
        telegram_id,
        charge_id="renewal-charge",
        period_end=second_end,
    )

    snapshot = await subscription_env.management.get_snapshot(user_id)

    assert snapshot.user_id == user_id
    assert snapshot.payer_telegram_id == telegram_id
    assert snapshot.status is SubscriptionStatus.ACTIVE
    assert snapshot.renewal_enabled
    assert snapshot.current_period_end == second_end
    assert snapshot.telegram_payment_charge_id == "stable-first-charge"


async def test_snapshot_uses_latest_first_charge_after_resubscription(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    first_end = NOW + timedelta(days=30)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id="former-subscription-charge",
        period_end=first_end,
    )
    subscription_env.clock.now = first_end
    second_end = first_end + timedelta(days=30)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id="current-subscription-charge",
        period_end=second_end,
    )

    snapshot = await subscription_env.management.get_snapshot(user_id)

    assert snapshot.current_period_end == second_end
    assert snapshot.telegram_payment_charge_id == "current-subscription-charge"


async def test_renewal_control_has_no_transaction_across_provider_io(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    period_end = NOW + timedelta(days=30)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id="control-charge",
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
            telegram_payment_charge_id="control-charge",
            enabled=False,
        )
    ]
    assert not result.renewal_enabled
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
        charge_id="failing-control-charge",
        period_end=NOW + timedelta(days=30),
    )

    class Provider:
        async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
            assert subscription_env.transactions.active == 0
            raise ConnectionError("provider unavailable")

    with pytest.raises(ConnectionError, match="provider unavailable"):
        await subscription_env.management.set_renewal(
            user_id,
            enabled=False,
            provider=Provider(),
        )

    snapshot = await subscription_env.management.get_snapshot(user_id)
    assert snapshot.renewal_enabled
    assert snapshot.status is SubscriptionStatus.ACTIVE


async def test_expired_cycle_is_rejected_before_provider_io(
    subscription_env: SubscriptionEnvironment,
) -> None:
    user_id, telegram_id = await _create_user(subscription_env)
    period_end = NOW + timedelta(days=30)
    await _start_subscription(
        subscription_env,
        user_id,
        telegram_id,
        charge_id="expired-control-charge",
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
