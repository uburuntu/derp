"""Subscription management across short persistence and provider boundaries."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.settlement import PaymentSettlementService
from derp.billing.types import (
    SubscriptionManagementSnapshot,
    SubscriptionRenewalCommand,
    SubscriptionStateError,
    SubscriptionStateResult,
    SubscriptionStatus,
)
from derp.models import PaymentReceipt, Subscription, SubscriptionCycle, User

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubscriptionRenewalProvider(Protocol):
    """Apply a renewal state to the external subscription provider."""

    async def set_renewal(self, command: SubscriptionRenewalCommand) -> None: ...


class SubscriptionManagementService:
    """Read and control personal plans without holding sessions across I/O."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._settlement = PaymentSettlementService(
            transactions,
            clock=clock,
        )
        self._clock = clock

    async def get_snapshot(
        self,
        user_id: uuid.UUID,
    ) -> SubscriptionManagementSnapshot:
        """Load current plan state and its stable provider subscription charge."""
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(Subscription, User.telegram_id)
                    .join(User, User.id == Subscription.user_id)
                    .where(Subscription.user_id == user_id)
                )
            ).one_or_none()
            if row is None:
                raise SubscriptionStateError("User has no subscription")
            subscription, payer_telegram_id = row
            charge_id = await session.scalar(
                select(PaymentReceipt.telegram_charge_id)
                .join(
                    SubscriptionCycle,
                    SubscriptionCycle.payment_receipt_id == PaymentReceipt.id,
                )
                .where(
                    SubscriptionCycle.subscription_id == subscription.id,
                    PaymentReceipt.is_first_recurring.is_(True),
                )
                .order_by(
                    SubscriptionCycle.period_start.desc(),
                    SubscriptionCycle.id.desc(),
                )
                .limit(1)
            )
            if charge_id is None:
                raise SubscriptionStateError(
                    "Subscription has no first recurring payment receipt"
                )
            return SubscriptionManagementSnapshot(
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                payer_telegram_id=payer_telegram_id,
                plan_id=subscription.plan_id,
                plan_version=subscription.plan_version,
                status=SubscriptionStatus(subscription.status),
                renewal_enabled=subscription.renewal_enabled,
                current_period_end=subscription.current_period_end,
                telegram_payment_charge_id=charge_id,
            )

    async def set_renewal(
        self,
        user_id: uuid.UUID,
        *,
        enabled: bool,
        provider: SubscriptionRenewalProvider,
    ) -> SubscriptionStateResult:
        """Apply provider state first, then persist the confirmed local state."""
        snapshot = await self.get_snapshot(user_id)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        if (
            snapshot.status is SubscriptionStatus.EXPIRED
            or snapshot.current_period_end <= now
        ):
            raise SubscriptionStateError("Subscription cycle has expired")
        await provider.set_renewal(
            SubscriptionRenewalCommand(
                payer_telegram_id=snapshot.payer_telegram_id,
                telegram_payment_charge_id=snapshot.telegram_payment_charge_id,
                enabled=enabled,
            )
        )
        return await self._settlement.set_subscription_renewal(
            user_id,
            enabled=enabled,
        )


__all__ = ["SubscriptionManagementService", "SubscriptionRenewalProvider"]
