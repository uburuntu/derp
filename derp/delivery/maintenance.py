"""Lifecycle-owned maintenance for recoverable Telegram deliveries."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from types import TracebackType
from typing import Final, Protocol, Self

import logfire

from derp.delivery.types import (
    ArtifactCleanup,
    Delivered,
    DeliveryFailed,
    DeliveryOutcome,
    DeliveryReconciliation,
    DeliveryUncertain,
)
from derp.observability import report_exception
from derp.operations.types import OperationId

DEFAULT_DELIVERY_MAINTENANCE_INTERVAL: Final = timedelta(minutes=5)
DEFAULT_DELIVERY_STALE_AFTER: Final = timedelta(minutes=15)
MIN_DELIVERY_STALE_AFTER: Final = timedelta(minutes=1)
MAX_DELIVERY_STALE_AFTER: Final = timedelta(days=1)
MAX_DELIVERY_MAINTENANCE_INTERVAL: Final = timedelta(days=1)
MAX_DELIVERY_MAINTENANCE_BATCH_SIZE: Final = 1_000
MAX_DELIVERY_MAINTENANCE_CONCURRENCY: Final = 32


class Clock(Protocol):
    """Injectable aware UTC clock for deterministic maintenance."""

    def __call__(self) -> datetime: ...


class DeliveryMaintenanceBackend(Protocol):
    """Narrow delivery surface used by the runtime worker."""

    async def reconcile_interrupted(
        self,
        *,
        stale_before: datetime,
        limit: int = 100,
    ) -> DeliveryReconciliation: ...

    async def pending_operation_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OperationId, ...]: ...

    async def deliver(self, operation_id: OperationId) -> DeliveryOutcome: ...

    async def reconcile_expired(
        self,
        *,
        limit: int = 100,
    ) -> DeliveryReconciliation: ...

    async def cleanup_expired_artifacts(
        self,
        *,
        limit: int = 100,
    ) -> ArtifactCleanup: ...


@dataclass(frozen=True, slots=True)
class DeliveryMaintenanceReport:
    """Identifiers-free counts from one bounded maintenance pass."""

    interrupted_count: int = 0
    retry_candidate_count: int = 0
    delivered_count: int = 0
    retryable_failure_count: int = 0
    terminal_failure_count: int = 0
    uncertain_count: int = 0
    retry_exception_count: int = 0
    expired_count: int = 0
    interruption_failure_count: int = 0
    expiration_failure_count: int = 0
    artifact_examined_count: int = 0
    artifact_purged_count: int = 0
    artifact_failure_count: int = 0
    phase_failure_count: int = 0

    def __post_init__(self) -> None:
        values = tuple(getattr(self, field.name) for field in fields(self))
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError(
                "delivery maintenance counts must be non-negative integers"
            )
        retry_outcomes = (
            self.delivered_count
            + self.retryable_failure_count
            + self.terminal_failure_count
            + self.uncertain_count
            + self.retry_exception_count
        )
        if retry_outcomes != self.retry_candidate_count:
            raise ValueError("retry outcomes must equal retry candidates")
        if self.interruption_failure_count > self.interrupted_count:
            raise ValueError("interruption failures exceed interrupted deliveries")
        if self.expiration_failure_count > self.expired_count:
            raise ValueError("expiration failures exceed expired deliveries")
        if (
            self.artifact_purged_count + self.artifact_failure_count
            > self.artifact_examined_count
        ):
            raise ValueError("artifact cleanup outcomes exceed examined artifacts")

    @property
    def operation_failure_count(self) -> int:
        """Operations whose maintenance did not finish in this pass."""
        return (
            self.retry_exception_count
            + self.interruption_failure_count
            + self.expiration_failure_count
        )

    @property
    def activity_count(self) -> int:
        """Rows or artifacts examined or changed by this pass."""
        return (
            self.interrupted_count
            + self.retry_candidate_count
            + self.expired_count
            + self.artifact_examined_count
        )

    @classmethod
    def empty(cls) -> Self:
        return cls()


@dataclass(slots=True)
class _MutableReport:
    interrupted_count: int = 0
    retry_candidate_count: int = 0
    delivered_count: int = 0
    retryable_failure_count: int = 0
    terminal_failure_count: int = 0
    uncertain_count: int = 0
    retry_exception_count: int = 0
    expired_count: int = 0
    interruption_failure_count: int = 0
    expiration_failure_count: int = 0
    artifact_examined_count: int = 0
    artifact_purged_count: int = 0
    artifact_failure_count: int = 0
    phase_failure_count: int = 0

    def freeze(self) -> DeliveryMaintenanceReport:
        return DeliveryMaintenanceReport(
            **{field.name: getattr(self, field.name) for field in fields(self)}
        )


class _RetryResult(Enum):
    DELIVERED = auto()
    RETRYABLE_FAILURE = auto()
    TERMINAL_FAILURE = auto()
    UNCERTAIN = auto()
    EXCEPTION = auto()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeliveryMaintenanceWorker:
    """Repair, retry, expire, and clean delivery state without blind replay.

    Recovery cannot edit a pre-crash progress message because its localized
    status text is not persisted with the delivery intent.
    """

    def __init__(
        self,
        delivery: DeliveryMaintenanceBackend,
        *,
        interval: timedelta = DEFAULT_DELIVERY_MAINTENANCE_INTERVAL,
        stale_after: timedelta = DEFAULT_DELIVERY_STALE_AFTER,
        batch_size: int = 100,
        concurrency: int = 4,
        clock: Clock = _utc_now,
    ) -> None:
        if interval <= timedelta(0) or interval > MAX_DELIVERY_MAINTENANCE_INTERVAL:
            raise ValueError("interval must be positive and at most one day")
        if not MIN_DELIVERY_STALE_AFTER <= stale_after <= MAX_DELIVERY_STALE_AFTER:
            raise ValueError("stale_after must be between one minute and one day")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= MAX_DELIVERY_MAINTENANCE_BATCH_SIZE
        ):
            raise ValueError(
                "batch_size must be between 1 and "
                f"{MAX_DELIVERY_MAINTENANCE_BATCH_SIZE}"
            )
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= MAX_DELIVERY_MAINTENANCE_CONCURRENCY
        ):
            raise ValueError("concurrency must be positive and bounded")
        self._delivery = delivery
        self._interval_seconds = interval.total_seconds()
        self._stale_after = stale_after
        self._batch_size = batch_size
        self._concurrency = concurrency
        self._clock = clock
        self._sweep_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("Delivery maintenance worker is already running")
        self._stop.clear()
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="delivery-maintenance",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> DeliveryMaintenanceReport:
        """Run one serialized pass while isolating phases and operations."""
        async with self._sweep_lock:
            report = _MutableReport()
            now = self._aware_now()

            try:
                interrupted = await self._delivery.reconcile_interrupted(
                    stale_before=now - self._stale_after,
                    limit=self._batch_size,
                )
                report.interrupted_count = interrupted.reconciled_count
                report.interruption_failure_count += interrupted.failed_count
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "delivery_maintenance_phase_failed",
                    exception=exc,
                    level="warning",
                    phase="interrupted_reconciliation",
                    failure_count=1,
                )
                report.phase_failure_count += 1

            try:
                candidates = await self._delivery.pending_operation_ids(
                    limit=self._batch_size
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "delivery_maintenance_phase_failed",
                    exception=exc,
                    level="warning",
                    phase="pending_discovery",
                    failure_count=1,
                )
                candidates = ()
                report.phase_failure_count += 1
            report.retry_candidate_count = len(candidates)
            self._record_retry_results(
                report,
                await self._retry_pending(candidates),
            )

            try:
                expired = await self._delivery.reconcile_expired(limit=self._batch_size)
                report.expired_count = expired.reconciled_count
                report.expiration_failure_count += expired.failed_count
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "delivery_maintenance_phase_failed",
                    exception=exc,
                    level="warning",
                    phase="expiration_reconciliation",
                    failure_count=1,
                )
                report.phase_failure_count += 1

            try:
                cleanup = await self._delivery.cleanup_expired_artifacts(
                    limit=self._batch_size
                )
                report.artifact_examined_count = cleanup.examined_count
                report.artifact_purged_count = cleanup.purged_count
                report.artifact_failure_count = cleanup.failed_count
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "delivery_maintenance_phase_failed",
                    exception=exc,
                    level="warning",
                    phase="artifact_cleanup",
                    failure_count=1,
                )
                report.phase_failure_count += 1

            result = report.freeze()
        self._log_result(result)
        return result

    async def aclose(self) -> None:
        """Signal and join maintenance before Telegram and database shutdown."""
        if (task := self._task) is None:
            return
        self._task = None
        self._stop.set()
        with suppress(asyncio.CancelledError):
            await task

    async def _retry_pending(
        self,
        operation_ids: tuple[OperationId, ...],
    ) -> tuple[_RetryResult, ...]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def retry(operation_id: OperationId) -> _RetryResult:
            async with semaphore:
                try:
                    outcome = await self._delivery.deliver(operation_id)
                    if isinstance(outcome, Delivered):
                        return _RetryResult.DELIVERED
                    if isinstance(outcome, DeliveryUncertain):
                        return _RetryResult.UNCERTAIN
                    if isinstance(outcome, DeliveryFailed):
                        if outcome.retryable:
                            return _RetryResult.RETRYABLE_FAILURE
                        return _RetryResult.TERMINAL_FAILURE
                    raise TypeError("Delivery backend returned an invalid outcome")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_exception(
                        "delivery_maintenance_operation_failed",
                        exception=exc,
                        level="warning",
                        phase="pending_retry",
                        failure_count=1,
                    )
                    return _RetryResult.EXCEPTION

        return tuple(await asyncio.gather(*(retry(value) for value in operation_ids)))

    async def _run_periodically(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                await self.sweep()

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Delivery maintenance clock must return an aware datetime")
        return now

    @staticmethod
    def _record_retry_results(
        report: _MutableReport,
        results: tuple[_RetryResult, ...],
    ) -> None:
        report.delivered_count = results.count(_RetryResult.DELIVERED)
        report.retryable_failure_count = results.count(_RetryResult.RETRYABLE_FAILURE)
        report.terminal_failure_count = results.count(_RetryResult.TERMINAL_FAILURE)
        report.uncertain_count = results.count(_RetryResult.UNCERTAIN)
        report.retry_exception_count = results.count(_RetryResult.EXCEPTION)

    @staticmethod
    def _log_result(report: DeliveryMaintenanceReport) -> None:
        if report.activity_count:
            logfire.info(
                "delivery_maintenance_completed",
                interrupted_count=report.interrupted_count,
                retry_candidate_count=report.retry_candidate_count,
                delivered_count=report.delivered_count,
                retryable_failure_count=report.retryable_failure_count,
                terminal_failure_count=report.terminal_failure_count,
                uncertain_count=report.uncertain_count,
                expired_count=report.expired_count,
                artifact_examined_count=report.artifact_examined_count,
                artifact_purged_count=report.artifact_purged_count,
            )
        if (
            report.operation_failure_count
            or report.artifact_failure_count
            or report.phase_failure_count
        ):
            logfire.warning(
                "delivery_maintenance_incomplete",
                operation_failure_count=report.operation_failure_count,
                artifact_failure_count=report.artifact_failure_count,
                phase_failure_count=report.phase_failure_count,
            )


__all__ = [
    "DEFAULT_DELIVERY_MAINTENANCE_INTERVAL",
    "DEFAULT_DELIVERY_STALE_AFTER",
    "DeliveryMaintenanceBackend",
    "DeliveryMaintenanceReport",
    "DeliveryMaintenanceWorker",
    "MAX_DELIVERY_MAINTENANCE_BATCH_SIZE",
    "MAX_DELIVERY_MAINTENANCE_CONCURRENCY",
    "MAX_DELIVERY_MAINTENANCE_INTERVAL",
    "MAX_DELIVERY_STALE_AFTER",
    "MIN_DELIVERY_STALE_AFTER",
]
