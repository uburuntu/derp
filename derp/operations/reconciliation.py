"""Bounded crash reconciliation for paid operation state."""

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
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from derp.common.tasks import task_is_running
from derp.models import Artifact, DeliveryIntent, OperationQuote, PaidOperation
from derp.observability import report_exception
from derp.operations.ledger import OperationLedger
from derp.operations.types import DeliveryState, OperationId, OperationState

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

DEFAULT_OPERATION_STALE_AFTER: Final = timedelta(minutes=30)
MIN_OPERATION_STALE_AFTER: Final = timedelta(minutes=10)
MAX_OPERATION_STALE_AFTER: Final = timedelta(days=1)
DEFAULT_RECONCILIATION_INTERVAL: Final = timedelta(minutes=5)
MAX_RECONCILIATION_INTERVAL: Final = timedelta(days=1)
MAX_RECONCILIATION_BATCH_SIZE: Final = 1_000

_STALE_RESERVATION_REASON: Final = "crash_reconciliation_stale_reservation"
_ORPHANED_EXECUTION_REASON: Final = "crash_reconciliation_orphaned_execution"


class Clock(Protocol):
    """Injectable UTC clock for deterministic reconciliation."""

    def __call__(self) -> datetime: ...


class DeliveryReadiness(Protocol):
    """Narrow delivery transition needed after recovered spend capture."""

    async def reconcile_ready(self, operation_id: OperationId) -> bool: ...


@dataclass(frozen=True, slots=True)
class OperationReconciliationReport:
    """Content-free outcomes from one bounded reconciliation pass."""

    examined_count: int = 0
    expired_quote_count: int = 0
    released_reservation_count: int = 0
    released_execution_count: int = 0
    recovered_delivery_count: int = 0
    incomplete_result_count: int = 0
    race_skipped_count: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.examined_count,
            self.expired_quote_count,
            self.released_reservation_count,
            self.released_execution_count,
            self.recovered_delivery_count,
            self.incomplete_result_count,
            self.race_skipped_count,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise ValueError("reconciliation counts must be non-negative integers")
        if (
            self.reconciled_count
            + self.incomplete_result_count
            + self.race_skipped_count
            > self.examined_count
        ):
            raise ValueError("reconciliation outcomes exceed examined operations")

    @property
    def reconciled_count(self) -> int:
        """Number of operations changed by this pass."""
        return (
            self.expired_quote_count
            + self.released_reservation_count
            + self.released_execution_count
            + self.recovered_delivery_count
        )

    @classmethod
    def empty(cls) -> Self:
        """Return a typed empty result for failed or idle worker sweeps."""
        return cls()


class OperationReconciliationRunner(Protocol):
    """Bounded pass contract owned by the lifecycle worker."""

    async def reconcile(
        self,
        *,
        limit: int = 100,
    ) -> OperationReconciliationReport: ...


@dataclass(slots=True)
class _MutableReport:
    examined_count: int = 0
    expired_quote_count: int = 0
    released_reservation_count: int = 0
    released_execution_count: int = 0
    recovered_delivery_count: int = 0
    incomplete_result_count: int = 0
    race_skipped_count: int = 0

    def freeze(self) -> OperationReconciliationReport:
        return OperationReconciliationReport(
            examined_count=self.examined_count,
            expired_quote_count=self.expired_quote_count,
            released_reservation_count=self.released_reservation_count,
            released_execution_count=self.released_execution_count,
            recovered_delivery_count=self.recovered_delivery_count,
            incomplete_result_count=self.incomplete_result_count,
            race_skipped_count=self.race_skipped_count,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OperationReconciler:
    """Close abandoned pre-capture work without resuming provider execution."""

    def __init__(
        self,
        transactions: TransactionFactory,
        delivery: DeliveryReadiness,
        *,
        clock: Clock = _utc_now,
        stale_after: timedelta = DEFAULT_OPERATION_STALE_AFTER,
    ) -> None:
        if not MIN_OPERATION_STALE_AFTER <= stale_after <= MAX_OPERATION_STALE_AFTER:
            raise ValueError("stale_after must be between ten minutes and one day")
        self._transactions = transactions
        self._delivery = delivery
        self._clock = clock
        self._stale_after = stale_after
        self._ledger = OperationLedger(transactions, clock=clock)

    async def reconcile(
        self,
        *,
        limit: int = 100,
    ) -> OperationReconciliationReport:
        """Reconcile at most ``limit`` operations in recovery-first order."""
        self._require_limit(limit)
        now = self._aware_now()
        stale_before = now - self._stale_after
        remaining = limit
        report = _MutableReport()

        remaining = await self._recover_persisted_results(report, remaining, now)
        remaining = await self._release_stale_executions(
            report,
            remaining,
            stale_before,
        )
        remaining = await self._release_stale_reservations(
            report,
            remaining,
            stale_before,
        )
        remaining = await self._expire_quotes(report, remaining, now)
        await self._surface_incomplete_results(report, remaining, stale_before)
        return report.freeze()

    async def _recover_persisted_results(
        self,
        report: _MutableReport,
        limit: int,
        now: datetime,
    ) -> int:
        if limit == 0:
            return 0
        candidates = await self._candidate_ids(
            select(PaidOperation.id)
            .join(
                DeliveryIntent,
                DeliveryIntent.operation_id == PaidOperation.id,
            )
            .where(
                PaidOperation.state.in_(
                    {
                        OperationState.EXECUTING.value,
                        OperationState.CAPTURED.value,
                    }
                ),
                PaidOperation.wallet_id.is_not(None),
                PaidOperation.created_at <= now,
                DeliveryIntent.state == DeliveryState.NOT_READY.value,
                exists().where(Artifact.operation_id == PaidOperation.id),
            )
            .order_by(PaidOperation.updated_at, PaidOperation.id),
            limit,
        )
        for operation_id in candidates:
            report.examined_count += 1
            settlement = await self._ledger.capture_persisted_result(operation_id)
            if settlement is None:
                report.race_skipped_count += 1
                continue
            ready_changed = await self._delivery.reconcile_ready(operation_id)
            if settlement.changed or ready_changed:
                report.recovered_delivery_count += 1
            else:
                report.race_skipped_count += 1
        return limit - len(candidates)

    async def _release_stale_executions(
        self,
        report: _MutableReport,
        limit: int,
        stale_before: datetime,
    ) -> int:
        if limit == 0:
            return 0
        candidates = await self._candidate_ids(
            select(PaidOperation.id)
            .where(
                PaidOperation.state == OperationState.EXECUTING.value,
                PaidOperation.wallet_id.is_not(None),
                PaidOperation.execution_started_at <= stale_before,
                ~exists().where(Artifact.operation_id == PaidOperation.id),
            )
            .order_by(PaidOperation.execution_started_at, PaidOperation.id),
            limit,
        )
        for operation_id in candidates:
            report.examined_count += 1
            result = await self._ledger.release_stale_execution_without_result(
                operation_id,
                stale_before=stale_before,
                reason=_ORPHANED_EXECUTION_REASON,
            )
            if result is None:
                report.race_skipped_count += 1
            elif result.changed:
                report.released_execution_count += 1
            else:
                report.race_skipped_count += 1
        return limit - len(candidates)

    async def _release_stale_reservations(
        self,
        report: _MutableReport,
        limit: int,
        stale_before: datetime,
    ) -> int:
        if limit == 0:
            return 0
        candidates = await self._candidate_ids(
            select(PaidOperation.id)
            .where(
                PaidOperation.state == OperationState.RESERVED.value,
                PaidOperation.wallet_id.is_not(None),
                PaidOperation.reserved_at <= stale_before,
            )
            .order_by(PaidOperation.reserved_at, PaidOperation.id),
            limit,
        )
        for operation_id in candidates:
            report.examined_count += 1
            result = await self._ledger.release_stale_reservation(
                operation_id,
                stale_before=stale_before,
                reason=_STALE_RESERVATION_REASON,
            )
            if result is None:
                report.race_skipped_count += 1
            elif result.changed:
                report.released_reservation_count += 1
            else:
                report.race_skipped_count += 1
        return limit - len(candidates)

    async def _expire_quotes(
        self,
        report: _MutableReport,
        limit: int,
        now: datetime,
    ) -> int:
        if limit == 0:
            return 0
        candidates = await self._candidate_ids(
            select(PaidOperation.id)
            .join(OperationQuote, OperationQuote.id == PaidOperation.quote_id)
            .where(
                PaidOperation.state == OperationState.QUOTED.value,
                OperationQuote.expires_at <= now,
            )
            .order_by(OperationQuote.expires_at, PaidOperation.id),
            limit,
        )
        for operation_id in candidates:
            report.examined_count += 1
            result = await self._ledger.expire_quote_if_due(
                operation_id,
                as_of=now,
            )
            if result is None:
                report.race_skipped_count += 1
            elif result.changed:
                report.expired_quote_count += 1
            else:
                report.race_skipped_count += 1
        return limit - len(candidates)

    async def _surface_incomplete_results(
        self,
        report: _MutableReport,
        limit: int,
        stale_before: datetime,
    ) -> None:
        """Surface impossible partial result commits without releasing spend."""
        if limit == 0:
            return
        candidates = await self._candidate_ids(
            select(PaidOperation.id)
            .where(
                PaidOperation.state == OperationState.EXECUTING.value,
                PaidOperation.wallet_id.is_not(None),
                PaidOperation.execution_started_at <= stale_before,
                exists().where(Artifact.operation_id == PaidOperation.id),
                ~exists().where(DeliveryIntent.operation_id == PaidOperation.id),
            )
            .order_by(PaidOperation.execution_started_at, PaidOperation.id),
            limit,
        )
        report.examined_count += len(candidates)
        report.incomplete_result_count += len(candidates)

    async def _candidate_ids(
        self,
        statement: Select[tuple[uuid.UUID]],
        limit: int,
    ) -> tuple[OperationId, ...]:
        async with self._transactions() as session:
            ids = tuple(
                await session.scalars(
                    statement.limit(limit).with_for_update(
                        of=PaidOperation,
                        skip_locked=True,
                    )
                )
            )
        return tuple(OperationId(value) for value in ids)

    @staticmethod
    def _require_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RECONCILIATION_BATCH_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_RECONCILIATION_BATCH_SIZE}"
            )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("OperationReconciler clock must return an aware datetime")
        return now


class OperationReconciliationWorker:
    """Run startup and periodic bounded operation reconciliation."""

    def __init__(
        self,
        reconciler: OperationReconciliationRunner,
        *,
        interval: timedelta = DEFAULT_RECONCILIATION_INTERVAL,
        batch_size: int = 100,
    ) -> None:
        if interval <= timedelta(0) or interval > MAX_RECONCILIATION_INTERVAL:
            raise ValueError("interval must be positive and at most one day")
        OperationReconciler._require_limit(batch_size)
        self._reconciler = reconciler
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
            raise RuntimeError("Operation reconciliation worker is already running")
        self._stop.clear()
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="operation-reconciliation",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> OperationReconciliationReport:
        """Run one serialized pass and report only identifiers-free counts."""
        async with self._sweep_lock:
            try:
                result = await self._reconciler.reconcile(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "operation_reconciliation_sweep_failed",
                    exception=exc,
                    level="warning",
                )
                return OperationReconciliationReport.empty()
        if result.reconciled_count:
            logfire.info(
                "paid_operations_reconciled",
                examined_count=result.examined_count,
                expired_quote_count=result.expired_quote_count,
                released_reservation_count=result.released_reservation_count,
                released_execution_count=result.released_execution_count,
                recovered_delivery_count=result.recovered_delivery_count,
                race_skipped_count=result.race_skipped_count,
            )
        if result.incomplete_result_count:
            logfire.warning(
                "paid_operation_result_incomplete",
                incomplete_result_count=result.incomplete_result_count,
            )
        return result

    async def aclose(self) -> None:
        """Signal and join the worker before database or delivery shutdown."""
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
    "DEFAULT_OPERATION_STALE_AFTER",
    "DEFAULT_RECONCILIATION_INTERVAL",
    "MAX_OPERATION_STALE_AFTER",
    "MAX_RECONCILIATION_BATCH_SIZE",
    "MAX_RECONCILIATION_INTERVAL",
    "MIN_OPERATION_STALE_AFTER",
    "DeliveryReadiness",
    "OperationReconciler",
    "OperationReconciliationReport",
    "OperationReconciliationRunner",
    "OperationReconciliationWorker",
]
