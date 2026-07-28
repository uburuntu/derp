"""Durable subscription renewal controls across Telegram and PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Final, Protocol, Self

import logfire
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.types import (
    SubscriptionManagementSnapshot,
    SubscriptionRenewalCommand,
    SubscriptionRenewalDisposition,
    SubscriptionStateError,
    SubscriptionStateResult,
    SubscriptionStatus,
)
from derp.common.tasks import task_is_running
from derp.models import (
    PaymentReceipt,
    Subscription,
    SubscriptionCycle,
    SubscriptionRenewalCommandRecord,
    User,
)
from derp.observability import report_exception

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

DEFAULT_SUBSCRIPTION_RENEWAL_LEASE: Final = timedelta(minutes=5)
DEFAULT_SUBSCRIPTION_RENEWAL_RETRY_INTERVAL: Final = timedelta(seconds=30)
DEFAULT_SUBSCRIPTION_RENEWAL_REPLAY_INTERVAL: Final = timedelta(seconds=30)
MAX_SUBSCRIPTION_RENEWAL_ATTEMPTS: Final = 8
MAX_SUBSCRIPTION_RENEWAL_BATCH_SIZE: Final = 50


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubscriptionRenewalProvider(Protocol):
    """Apply one absolute renewal state to the external provider."""

    async def set_renewal(self, command: SubscriptionRenewalCommand) -> None: ...


@dataclass(frozen=True, slots=True)
class SubscriptionRenewalSweepReport:
    """Identifier-free result of one bounded recovery pass."""

    claimed_count: int = 0
    applied_count: int = 0
    retry_scheduled_count: int = 0
    attention_count: int = 0
    superseded_count: int = 0


@dataclass(frozen=True, slots=True)
class _RenewalClaim:
    command_id: uuid.UUID
    subscription_id: uuid.UUID
    lease_token: uuid.UUID
    payer_telegram_id: int
    telegram_charge_id: str
    desired_enabled: bool
    attempt_count: int


class SubscriptionManagementService:
    """Read plans and reconcile idempotent provider renewal commands."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        clock: Clock = _utc_now,
        lease_duration: timedelta = DEFAULT_SUBSCRIPTION_RENEWAL_LEASE,
        retry_interval: timedelta = DEFAULT_SUBSCRIPTION_RENEWAL_RETRY_INTERVAL,
        max_attempts: int = MAX_SUBSCRIPTION_RENEWAL_ATTEMPTS,
    ) -> None:
        if not callable(transactions):
            raise TypeError("transactions must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not timedelta(0) < lease_duration <= timedelta(hours=1):
            raise ValueError("lease_duration must be positive and at most one hour")
        if not timedelta(0) < retry_interval <= timedelta(hours=1):
            raise ValueError("retry_interval must be positive and at most one hour")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 32
        ):
            raise ValueError("max_attempts must be between 1 and 32")
        self._transactions = transactions
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_interval = retry_interval
        self._max_attempts = max_attempts

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
            charge_id = await self._latest_first_charge(session, subscription.id)
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
        """Persist the desired state before provider I/O, then reconcile it."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        command_id, immediate = await self._prepare_command(user_id, enabled)
        if immediate is not None:
            return immediate
        if command_id is None:  # pragma: no cover - preparation invariant
            raise RuntimeError("renewal command preparation returned no outcome")
        claim, disposition = await self._claim_one(command_id)
        if claim is None:
            return await self._current_state(user_id, disposition)
        return await self.process_claim(claim, provider)

    async def claim_due(self, *, limit: int = 20) -> tuple[_RenewalClaim, ...]:
        """Lease a bounded batch, including commands abandoned during a crash."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SUBSCRIPTION_RENEWAL_BATCH_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_SUBSCRIPTION_RENEWAL_BATCH_SIZE}"
            )
        now = self._aware_now()
        async with self._transactions() as session:
            rows = list(
                await session.scalars(
                    select(SubscriptionRenewalCommandRecord)
                    .where(
                        or_(
                            (
                                (SubscriptionRenewalCommandRecord.status == "pending")
                                & (
                                    SubscriptionRenewalCommandRecord.next_attempt_at
                                    <= now
                                )
                            ),
                            (
                                (
                                    SubscriptionRenewalCommandRecord.status
                                    == "processing"
                                )
                                & (
                                    SubscriptionRenewalCommandRecord.lease_expires_at
                                    <= now
                                )
                            ),
                        )
                    )
                    .order_by(
                        SubscriptionRenewalCommandRecord.next_attempt_at,
                        SubscriptionRenewalCommandRecord.created_at,
                        SubscriptionRenewalCommandRecord.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            return tuple(self._lease(row, now) for row in rows)

    async def process_claim(
        self,
        claim: _RenewalClaim,
        provider: SubscriptionRenewalProvider,
    ) -> SubscriptionStateResult:
        """Apply one lease; absolute-state provider calls are safe to replay."""
        try:
            await provider.set_renewal(
                SubscriptionRenewalCommand(
                    payer_telegram_id=claim.payer_telegram_id,
                    telegram_payment_charge_id=claim.telegram_charge_id,
                    enabled=claim.desired_enabled,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report_exception(
                "subscription_renewal_provider_failed",
                exception=exc,
                level="warning",
            )
            return await self._retry_or_attention(claim)
        return await self._apply_claim(claim)

    async def _prepare_command(
        self,
        user_id: uuid.UUID,
        enabled: bool,
    ) -> tuple[uuid.UUID | None, SubscriptionStateResult | None]:
        now = self._aware_now()
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(Subscription, User.telegram_id)
                    .join(User, User.id == Subscription.user_id)
                    .where(Subscription.user_id == user_id)
                    .with_for_update(of=Subscription)
                )
            ).one_or_none()
            if row is None:
                raise SubscriptionStateError("User has no subscription")
            subscription, payer_telegram_id = row
            if (
                subscription.status == SubscriptionStatus.EXPIRED.value
                or subscription.current_period_end <= now
            ):
                raise SubscriptionStateError("Subscription cycle has expired")

            active = await session.scalar(
                select(SubscriptionRenewalCommandRecord)
                .where(
                    SubscriptionRenewalCommandRecord.subscription_id == subscription.id,
                    SubscriptionRenewalCommandRecord.status.in_(
                        ("pending", "processing", "attention")
                    ),
                )
                .with_for_update()
            )
            if active is not None and active.status == "processing":
                if active.desired_enabled is not enabled:
                    active.desired_enabled = enabled
                    active.attempt_count = 0
                    active.last_failure_code = None
                return None, self._result(
                    subscription,
                    SubscriptionRenewalDisposition.PENDING,
                )
            if active is not None and active.desired_enabled is enabled:
                if active.status == "attention":
                    return None, self._result(
                        subscription,
                        SubscriptionRenewalDisposition.ATTENTION,
                    )
                return active.id, None
            if active is not None:
                active.status = "superseded"
                active.completed_at = now
                active.lease_token = None
                active.lease_expires_at = None

            if subscription.renewal_enabled is enabled:
                return None, self._result(
                    subscription,
                    SubscriptionRenewalDisposition.UNCHANGED,
                )
            charge_id = await self._latest_first_charge(session, subscription.id)
            if charge_id is None:
                raise SubscriptionStateError(
                    "Subscription has no first recurring payment receipt"
                )
            command = SubscriptionRenewalCommandRecord(
                subscription_id=subscription.id,
                payer_telegram_id=payer_telegram_id,
                telegram_charge_id=charge_id,
                desired_enabled=enabled,
                status="pending",
                attempt_count=0,
                next_attempt_at=now,
            )
            session.add(command)
            await session.flush()
            return command.id, None

    async def _claim_one(
        self,
        command_id: uuid.UUID,
    ) -> tuple[_RenewalClaim | None, SubscriptionRenewalDisposition]:
        now = self._aware_now()
        async with self._transactions() as session:
            row = await session.scalar(
                select(SubscriptionRenewalCommandRecord)
                .where(SubscriptionRenewalCommandRecord.id == command_id)
                .with_for_update()
            )
            if row is None:
                return None, SubscriptionRenewalDisposition.SUPERSEDED
            if row.status == "applied":
                return None, SubscriptionRenewalDisposition.APPLIED
            if row.status == "superseded":
                return None, SubscriptionRenewalDisposition.SUPERSEDED
            if row.status == "attention":
                return None, SubscriptionRenewalDisposition.ATTENTION
            if row.status == "processing" and row.lease_expires_at > now:
                return None, SubscriptionRenewalDisposition.PENDING
            if row.status == "pending" and row.next_attempt_at > now:
                return None, SubscriptionRenewalDisposition.PENDING
            return self._lease(row, now), SubscriptionRenewalDisposition.PENDING

    def _lease(
        self,
        row: SubscriptionRenewalCommandRecord,
        now: datetime,
    ) -> _RenewalClaim:
        row.status = "processing"
        row.attempt_count += 1
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = now + self._lease_duration
        return _RenewalClaim(
            command_id=row.id,
            subscription_id=row.subscription_id,
            lease_token=row.lease_token,
            payer_telegram_id=row.payer_telegram_id,
            telegram_charge_id=row.telegram_charge_id,
            desired_enabled=row.desired_enabled,
            attempt_count=row.attempt_count,
        )

    async def _apply_claim(
        self,
        claim: _RenewalClaim,
    ) -> SubscriptionStateResult:
        now = self._aware_now()
        async with self._transactions() as session:
            subscription = await session.scalar(
                select(Subscription)
                .where(Subscription.id == claim.subscription_id)
                .with_for_update()
            )
            if subscription is None:
                raise SubscriptionStateError("Subscription no longer exists")
            command = await self._locked_claim(session, claim)
            if command.desired_enabled is not claim.desired_enabled:
                self._requeue_retargeted(command, now)
                return self._result(
                    subscription,
                    SubscriptionRenewalDisposition.PENDING,
                )
            current_charge = await self._latest_first_charge(session, subscription.id)
            if current_charge != claim.telegram_charge_id:
                self._complete(command, "superseded", now)
                return self._result(
                    subscription,
                    SubscriptionRenewalDisposition.SUPERSEDED,
                )
            current_cycle_status = await session.scalar(
                select(SubscriptionCycle.status)
                .where(
                    SubscriptionCycle.subscription_id == subscription.id,
                    SubscriptionCycle.period_end == subscription.current_period_end,
                )
                .limit(1)
            )

            subscription.renewal_enabled = claim.desired_enabled
            if (
                subscription.current_period_end <= now
                or current_cycle_status != "active"
            ):
                subscription.status = "expired"
            elif claim.desired_enabled:
                subscription.status = "active"
            else:
                subscription.status = "canceled"
            subscription.canceled_at = (
                None if claim.desired_enabled else subscription.canceled_at or now
            )
            self._complete(command, "applied", now)
            return self._result(
                subscription,
                SubscriptionRenewalDisposition.APPLIED,
            )

    async def _retry_or_attention(
        self,
        claim: _RenewalClaim,
    ) -> SubscriptionStateResult:
        now = self._aware_now()
        async with self._transactions() as session:
            subscription = await session.scalar(
                select(Subscription)
                .where(Subscription.id == claim.subscription_id)
                .with_for_update()
            )
            if subscription is None:
                raise SubscriptionStateError("Subscription no longer exists")
            command = await self._locked_claim(session, claim)
            if (
                command.desired_enabled is not claim.desired_enabled
                or command.attempt_count != claim.attempt_count
            ):
                self._requeue_retargeted(command, now)
                return self._result(
                    subscription,
                    SubscriptionRenewalDisposition.PENDING,
                )
            command.lease_token = None
            command.lease_expires_at = None
            command.last_failure_code = "provider_call_failed"
            if claim.attempt_count >= self._max_attempts:
                command.status = "attention"
                return self._result(
                    subscription,
                    SubscriptionRenewalDisposition.ATTENTION,
                )
            multiplier = 2 ** max(claim.attempt_count - 1, 0)
            command.status = "pending"
            command.next_attempt_at = now + min(
                self._retry_interval * multiplier,
                timedelta(minutes=30),
            )
            return self._result(
                subscription,
                SubscriptionRenewalDisposition.PENDING,
            )

    async def _current_state(
        self,
        user_id: uuid.UUID,
        disposition: SubscriptionRenewalDisposition,
    ) -> SubscriptionStateResult:
        async with self._transactions() as session:
            subscription = await session.scalar(
                select(Subscription).where(Subscription.user_id == user_id)
            )
            if subscription is None:
                raise SubscriptionStateError("User has no subscription")
            return self._result(subscription, disposition)

    @staticmethod
    async def _locked_claim(
        session: AsyncSession,
        claim: _RenewalClaim,
    ) -> SubscriptionRenewalCommandRecord:
        row = await session.scalar(
            select(SubscriptionRenewalCommandRecord)
            .where(
                SubscriptionRenewalCommandRecord.id == claim.command_id,
                SubscriptionRenewalCommandRecord.status == "processing",
                SubscriptionRenewalCommandRecord.lease_token == claim.lease_token,
            )
            .with_for_update()
        )
        if row is None:
            raise SubscriptionStateError("Subscription renewal lease was lost")
        return row

    @staticmethod
    async def _latest_first_charge(
        session: AsyncSession,
        subscription_id: uuid.UUID,
    ) -> str | None:
        return await session.scalar(
            select(PaymentReceipt.telegram_charge_id)
            .join(
                SubscriptionCycle,
                SubscriptionCycle.payment_receipt_id == PaymentReceipt.id,
            )
            .where(
                SubscriptionCycle.subscription_id == subscription_id,
                PaymentReceipt.is_first_recurring.is_(True),
            )
            .order_by(
                SubscriptionCycle.period_start.desc(),
                SubscriptionCycle.id.desc(),
            )
            .limit(1)
        )

    @staticmethod
    def _complete(
        command: SubscriptionRenewalCommandRecord,
        status: str,
        now: datetime,
    ) -> None:
        command.status = status
        command.completed_at = now
        command.last_failure_code = None
        command.lease_token = None
        command.lease_expires_at = None

    @staticmethod
    def _requeue_retargeted(
        command: SubscriptionRenewalCommandRecord,
        now: datetime,
    ) -> None:
        command.status = "pending"
        command.attempt_count = 0
        command.next_attempt_at = now
        command.last_failure_code = None
        command.lease_token = None
        command.lease_expires_at = None

    @staticmethod
    def _result(
        subscription: Subscription,
        disposition: SubscriptionRenewalDisposition,
    ) -> SubscriptionStateResult:
        return SubscriptionStateResult(
            subscription.id,
            subscription.renewal_enabled,
            subscription.current_period_end,
            disposition,
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now


class SubscriptionRenewalWorker:
    """Replay pending or crash-abandoned provider renewal commands."""

    def __init__(
        self,
        service: SubscriptionManagementService,
        provider: SubscriptionRenewalProvider,
        *,
        interval: timedelta = DEFAULT_SUBSCRIPTION_RENEWAL_REPLAY_INTERVAL,
        batch_size: int = 20,
    ) -> None:
        if not timedelta(0) < interval <= timedelta(days=1):
            raise ValueError("interval must be positive and at most one day")
        if (
            isinstance(batch_size, bool)
            or not 1 <= batch_size <= MAX_SUBSCRIPTION_RENEWAL_BATCH_SIZE
        ):
            raise ValueError(
                f"batch_size must be between 1 and {MAX_SUBSCRIPTION_RENEWAL_BATCH_SIZE}"
            )
        self._service = service
        self._provider = provider
        self._interval_seconds = interval.total_seconds()
        self._batch_size = batch_size
        self._sweep_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return task_is_running(self._task)

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("Subscription renewal worker is already running")
        self._stop.clear()
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="subscription-renewal-replay",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> SubscriptionRenewalSweepReport:
        """Reconcile one serialized batch and report aggregate outcomes."""
        async with self._sweep_lock:
            try:
                claims = await self._service.claim_due(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "subscription_renewal_claim_failed",
                    exception=exc,
                    level="warning",
                )
                return SubscriptionRenewalSweepReport()

            outcomes: list[SubscriptionRenewalDisposition] = []
            for claim in claims:
                try:
                    result = await self._service.process_claim(claim, self._provider)
                    outcomes.append(result.disposition)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_exception(
                        "subscription_renewal_replay_failed",
                        exception=exc,
                        level="warning",
                    )

        report = SubscriptionRenewalSweepReport(
            claimed_count=len(claims),
            applied_count=outcomes.count(SubscriptionRenewalDisposition.APPLIED),
            retry_scheduled_count=outcomes.count(
                SubscriptionRenewalDisposition.PENDING
            ),
            attention_count=outcomes.count(SubscriptionRenewalDisposition.ATTENTION),
            superseded_count=outcomes.count(SubscriptionRenewalDisposition.SUPERSEDED),
        )
        if report.claimed_count:
            logfire.info(
                "subscription_renewal_sweep",
                claimed_count=report.claimed_count,
                applied_count=report.applied_count,
                retry_scheduled_count=report.retry_scheduled_count,
                attention_count=report.attention_count,
                superseded_count=report.superseded_count,
            )
        return report

    async def aclose(self) -> None:
        if (task := self._task) is None:
            return
        self._task = None
        self._stop.set()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_periodically(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                await self.sweep()


__all__ = [
    "SubscriptionManagementService",
    "SubscriptionRenewalProvider",
    "SubscriptionRenewalSweepReport",
    "SubscriptionRenewalWorker",
]
