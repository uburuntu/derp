"""Aggregate-only diagnostics and bounded maintenance for bot operators."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast

import logfire
from sqlalchemy import Select, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from derp.approvals.maintenance import DeferredApprovalExpiryWorker
from derp.approvals.types import DeferredToolStatus
from derp.billing import SubscriptionExpiryWorker
from derp.billing.types import SubscriptionStatus
from derp.db import DatabaseManager
from derp.delivery import DeliveryMaintenanceReport, DeliveryMaintenanceWorker
from derp.history.retention import HistoryRetentionWorker
from derp.models import (
    Artifact,
    Chat,
    DeferredToolRequest,
    DeliveryIntent,
    Message,
    PaidOperation,
    PaymentReceipt,
    PurchaseIntent,
    Subscription,
    User,
    Wallet,
    WalletLot,
)
from derp.observability import report_exception
from derp.operations import (
    DeliveryState,
    OperationReconciliationReport,
    OperationReconciliationWorker,
    OperationState,
)
from derp.operator.types import (
    OperatorActivityTotals,
    OperatorArtifactTotals,
    OperatorConsoleSnapshot,
    OperatorDatabaseSnapshot,
    OperatorDatabaseStatus,
    OperatorMaintenanceAction,
    OperatorMaintenancePass,
    OperatorMaintenanceResult,
    OperatorNamedCount,
    OperatorPoolSnapshot,
    OperatorRuntimeSnapshot,
    OperatorStarsTotals,
    OperatorSubscriptionTotals,
    OperatorWalletTotals,
    OperatorWorkerStatus,
)

type MonotonicClock = Callable[[], float]
type UtcClock = Callable[[], datetime]


class _PoolGauges(Protocol):
    def size(self) -> int: ...

    def checkedin(self) -> int: ...

    def checkedout(self) -> int: ...

    def overflow(self) -> int: ...


_WINDOW_24H: Final = timedelta(hours=24)
_CHAT_TYPES: Final = ("private", "group", "supergroup", "channel")
_INTENT_STATES: Final = (
    "pending",
    "prechecked",
    "fulfilled",
    "expired",
    "canceled",
    "needs_review",
)
_RECEIPT_STATES: Final = (
    "received",
    "fulfilled",
    "clawed_back",
    "needs_review",
)
_WORKER_ACTIONS: Final = (
    OperatorMaintenanceAction.HISTORY,
    OperatorMaintenanceAction.SUBSCRIPTIONS,
    OperatorMaintenanceAction.OPERATIONS,
    OperatorMaintenanceAction.DELIVERIES,
    OperatorMaintenanceAction.APPROVALS,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OperatorConsoleService:
    """Read safe runtime aggregates and invoke existing maintenance workers."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        history_retention: HistoryRetentionWorker,
        subscription_expiry: SubscriptionExpiryWorker,
        operation_reconciliation: OperationReconciliationWorker,
        delivery_maintenance: DeliveryMaintenanceWorker,
        approval_expiry: DeferredApprovalExpiryWorker,
        monotonic_clock: MonotonicClock = time.monotonic,
        utc_clock: UtcClock = _utc_now,
    ) -> None:
        if not callable(monotonic_clock) or not callable(utc_clock):
            raise TypeError("operator console clocks must be callable")
        self._db = db
        self._history_retention = history_retention
        self._subscription_expiry = subscription_expiry
        self._operation_reconciliation = operation_reconciliation
        self._delivery_maintenance = delivery_maintenance
        self._approval_expiry = approval_expiry
        self._monotonic_clock = monotonic_clock
        self._utc_clock = utc_clock
        self._started_at = monotonic_clock()
        self._maintenance_lock = asyncio.Lock()

    async def snapshot(self) -> OperatorConsoleSnapshot:
        """Return runtime state and aggregate-only database diagnostics."""
        runtime = self._runtime_snapshot()
        try:
            database = await self._database_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report_exception(
                "operator.snapshot_degraded",
                exception=exc,
                level="warning",
            )
            database = OperatorDatabaseSnapshot.degraded()
        return OperatorConsoleSnapshot(runtime=runtime, database=database)

    async def run_maintenance(
        self,
        action: OperatorMaintenanceAction,
        actor_id: int,
    ) -> OperatorMaintenanceResult:
        """Run exact live worker passes under one service-wide serialization lock."""
        if not isinstance(action, OperatorMaintenanceAction):
            raise TypeError("action must be an OperatorMaintenanceAction")
        if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id <= 0:
            raise ValueError("actor_id must be a positive integer")

        async with self._maintenance_lock:
            started_at = self._monotonic_clock()
            actions = (
                _WORKER_ACTIONS
                if action is OperatorMaintenanceAction.ALL
                else (action,)
            )
            passes = tuple([await self._run_pass(item) for item in actions])
            result = OperatorMaintenanceResult(
                requested_action=action,
                passes=passes,
                duration_ms=self._elapsed_ms(started_at),
            )

        logfire.info(
            "operator.maintenance_pass_completed",
            actor_id=actor_id,
            action=action.value,
            completion="completed_pass",
            counts={
                item.action.value: {count.name: count.count for count in item.counts}
                for item in result.passes
            },
            duration_ms=result.duration_ms,
        )
        return result

    def _runtime_snapshot(self) -> OperatorRuntimeSnapshot:
        return OperatorRuntimeSnapshot(
            uptime_seconds=self._elapsed_seconds(self._started_at),
            workers=(
                OperatorWorkerStatus(
                    OperatorMaintenanceAction.HISTORY,
                    self._history_retention.is_running,
                ),
                OperatorWorkerStatus(
                    OperatorMaintenanceAction.SUBSCRIPTIONS,
                    self._subscription_expiry.is_running,
                ),
                OperatorWorkerStatus(
                    OperatorMaintenanceAction.OPERATIONS,
                    self._operation_reconciliation.is_running,
                ),
                OperatorWorkerStatus(
                    OperatorMaintenanceAction.DELIVERIES,
                    self._delivery_maintenance.is_running,
                ),
                OperatorWorkerStatus(
                    OperatorMaintenanceAction.APPROVALS,
                    self._approval_expiry.is_running,
                ),
            ),
        )

    async def _database_snapshot(self) -> OperatorDatabaseSnapshot:
        now = self._utc_clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("operator console UTC clock must return an aware datetime")
        cutoff = now - _WINDOW_24H

        async with self._db.read_session() as session:
            ping_started_at = self._monotonic_clock()
            await session.scalar(select(literal(1)))
            latency_ms = self._elapsed_ms(ping_started_at)

            users = await self._activity_totals(session, User, User.updated_at, cutoff)
            chats = await self._activity_totals(session, Chat, Chat.updated_at, cutoff)
            retained_messages = await self._activity_totals(
                session,
                Message,
                Message.telegram_date,
                cutoff,
            )
            chats_by_type = await self._grouped_counts(
                session,
                select(Chat.type, func.count()).group_by(Chat.type),
                _CHAT_TYPES,
            )
            wallet = await self._wallet_totals(session)
            operation_states = await self._grouped_counts(
                session,
                select(PaidOperation.state, func.count()).group_by(PaidOperation.state),
                tuple(state.value for state in OperationState),
            )
            delivery_states = await self._grouped_counts(
                session,
                select(DeliveryIntent.state, func.count()).group_by(
                    DeliveryIntent.state
                ),
                tuple(state.value for state in DeliveryState),
            )
            approval_states = await self._grouped_counts(
                session,
                select(DeferredToolRequest.status, func.count()).group_by(
                    DeferredToolRequest.status
                ),
                tuple(status.value for status in DeferredToolStatus),
            )
            intent_states = await self._grouped_counts(
                session,
                select(PurchaseIntent.status, func.count()).group_by(
                    PurchaseIntent.status
                ),
                _INTENT_STATES,
            )
            receipt_states = await self._grouped_counts(
                session,
                select(PaymentReceipt.status, func.count()).group_by(
                    PaymentReceipt.status
                ),
                _RECEIPT_STATES,
            )
            stars = await self._stars_totals(session)
            subscription_row = (
                await session.execute(
                    select(
                        func.count().filter(
                            Subscription.status == SubscriptionStatus.ACTIVE.value
                        ),
                        func.count().filter(
                            Subscription.status.in_(
                                (
                                    SubscriptionStatus.ACTIVE.value,
                                    SubscriptionStatus.CANCELED.value,
                                )
                            ),
                            Subscription.current_period_end > now,
                        ),
                        func.count().filter(
                            Subscription.status == SubscriptionStatus.ACTIVE.value,
                            Subscription.renewal_enabled.is_(True),
                            Subscription.current_period_end > now,
                        ),
                    ).select_from(Subscription)
                )
            ).one()
            subscriptions = OperatorSubscriptionTotals(
                status_active=int(subscription_row[0]),
                entitled=int(subscription_row[1]),
                auto_renewing=int(subscription_row[2]),
            )
            artifact_row = (
                await session.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(Artifact.size_bytes), 0),
                    ).select_from(Artifact)
                )
            ).one()
            artifacts = OperatorArtifactTotals(
                count=int(artifact_row[0]),
                bytes=int(artifact_row[1]),
            )

        return OperatorDatabaseSnapshot(
            status=OperatorDatabaseStatus.READY,
            latency_ms=latency_ms,
            pool=self._pool_snapshot(),
            users=users,
            chats=chats,
            retained_messages=retained_messages,
            chats_by_type=chats_by_type,
            wallet=wallet,
            operation_states=operation_states,
            delivery_states=delivery_states,
            approval_states=approval_states,
            intent_states=intent_states,
            receipt_states=receipt_states,
            stars=stars,
            subscriptions=subscriptions,
            artifacts=artifacts,
        )

    @staticmethod
    async def _activity_totals(
        session: AsyncSession,
        model: type[User] | type[Chat] | type[Message],
        activity_column: InstrumentedAttribute[datetime],
        cutoff: datetime,
    ) -> OperatorActivityTotals:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count().filter(activity_column >= cutoff),
                ).select_from(model)
            )
        ).one()
        return OperatorActivityTotals(total=int(row[0]), recent_24h=int(row[1]))

    @staticmethod
    async def _grouped_counts(
        session: AsyncSession,
        statement: Select[tuple[str, int]],
        vocabulary: tuple[str, ...],
    ) -> tuple[OperatorNamedCount, ...]:
        rows = (await session.execute(statement)).all()
        observed = {str(row[0]): int(row[1]) for row in rows}
        if set(observed) - set(vocabulary):
            raise ValueError("database contains an unsupported aggregate state")
        return tuple(
            OperatorNamedCount(name, observed.get(name, 0)) for name in vocabulary
        )

    @staticmethod
    async def _wallet_totals(session: AsyncSession) -> OperatorWalletTotals:
        lot_row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(WalletLot.available_credits), 0),
                    func.coalesce(func.sum(WalletLot.reserved_credits), 0),
                    func.coalesce(func.sum(WalletLot.consumed_credits), 0),
                )
            )
        ).one()
        debt = int(
            await session.scalar(
                select(func.coalesce(func.sum(Wallet.debt_credits), 0))
            )
            or 0
        )
        return OperatorWalletTotals(
            available_credits=int(lot_row[0]),
            reserved_credits=int(lot_row[1]),
            consumed_credits=int(lot_row[2]),
            debt_credits=debt,
        )

    @staticmethod
    async def _stars_totals(session: AsyncSession) -> OperatorStarsTotals:
        row = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(PaymentReceipt.total_amount).filter(
                            PaymentReceipt.status == "fulfilled"
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(PaymentReceipt.total_amount).filter(
                            PaymentReceipt.status == "clawed_back"
                        ),
                        0,
                    ),
                )
            )
        ).one()
        return OperatorStarsTotals(fulfilled=int(row[0]), clawed_back=int(row[1]))

    def _pool_snapshot(self) -> OperatorPoolSnapshot:
        pool = cast(_PoolGauges, self._db.engine.pool)
        checked_in = pool.checkedin()
        checked_out = pool.checkedout()
        return OperatorPoolSnapshot(
            size=pool.size(),
            checked_in=checked_in,
            checked_out=checked_out,
            overflow=max(0, pool.overflow()),
            open_connections=checked_in + checked_out,
        )

    async def _run_pass(
        self,
        action: OperatorMaintenanceAction,
    ) -> OperatorMaintenancePass:
        if action is OperatorMaintenanceAction.HISTORY:
            count = await self._history_retention.sweep()
            counts = self._counts(purged_message_count=count)
        elif action is OperatorMaintenanceAction.SUBSCRIPTIONS:
            count = await self._subscription_expiry.sweep()
            counts = self._counts(expired_cycle_count=count)
        elif action is OperatorMaintenanceAction.OPERATIONS:
            counts = self._operation_counts(
                await self._operation_reconciliation.sweep()
            )
        elif action is OperatorMaintenanceAction.DELIVERIES:
            counts = self._delivery_counts(await self._delivery_maintenance.sweep())
        elif action is OperatorMaintenanceAction.APPROVALS:
            count = await self._approval_expiry.sweep()
            counts = self._counts(expired_request_count=count)
        else:
            raise ValueError("all cannot be dispatched as an individual pass")
        return OperatorMaintenancePass(action=action, counts=counts)

    @classmethod
    def _operation_counts(
        cls,
        report: OperationReconciliationReport,
    ) -> tuple[OperatorNamedCount, ...]:
        return cls._counts(
            examined_count=report.examined_count,
            expired_quote_count=report.expired_quote_count,
            released_reservation_count=report.released_reservation_count,
            released_execution_count=report.released_execution_count,
            recovered_delivery_count=report.recovered_delivery_count,
            incomplete_result_count=report.incomplete_result_count,
            race_skipped_count=report.race_skipped_count,
        )

    @classmethod
    def _delivery_counts(
        cls,
        report: DeliveryMaintenanceReport,
    ) -> tuple[OperatorNamedCount, ...]:
        return cls._counts(
            interrupted_count=report.interrupted_count,
            retry_candidate_count=report.retry_candidate_count,
            delivered_count=report.delivered_count,
            retryable_failure_count=report.retryable_failure_count,
            terminal_failure_count=report.terminal_failure_count,
            uncertain_count=report.uncertain_count,
            retry_exception_count=report.retry_exception_count,
            expired_count=report.expired_count,
            interruption_failure_count=report.interruption_failure_count,
            expiration_failure_count=report.expiration_failure_count,
            artifact_examined_count=report.artifact_examined_count,
            artifact_purged_count=report.artifact_purged_count,
            artifact_failure_count=report.artifact_failure_count,
            phase_failure_count=report.phase_failure_count,
        )

    @staticmethod
    def _counts(**values: int) -> tuple[OperatorNamedCount, ...]:
        return tuple(OperatorNamedCount(name, value) for name, value in values.items())

    def _elapsed_seconds(self, started_at: float) -> float:
        elapsed = self._monotonic_clock() - started_at
        if elapsed < 0:
            raise RuntimeError("operator console monotonic clock moved backwards")
        return elapsed

    def _elapsed_ms(self, started_at: float) -> float:
        return self._elapsed_seconds(started_at) * 1_000


__all__ = ["OperatorConsoleService"]
