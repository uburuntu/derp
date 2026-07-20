"""Lifecycle and privacy contracts for operation reconciliation workers."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from derp.operations import (
    OperationReconciliationReport,
    OperationReconciliationWorker,
)


class RecordingReconciler:
    def __init__(
        self,
        results: list[OperationReconciliationReport | BaseException],
    ) -> None:
        self._results = iter(results)
        self.limits: list[int] = []
        self.called = asyncio.Event()

    async def reconcile(self, *, limit: int = 100) -> OperationReconciliationReport:
        self.limits.append(limit)
        result = next(self._results)
        self.called.set()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize(
    ("interval", "batch_size"),
    [
        (timedelta(0), 100),
        (timedelta(days=1, microseconds=1), 100),
        (timedelta(minutes=5), 0),
        (timedelta(minutes=5), 1_001),
    ],
)
def test_worker_rejects_unbounded_configuration(
    interval: timedelta,
    batch_size: int,
) -> None:
    with pytest.raises(ValueError):
        OperationReconciliationWorker(
            RecordingReconciler([]),
            interval=interval,
            batch_size=batch_size,
        )


def test_report_rejects_impossible_or_untyped_counts() -> None:
    with pytest.raises(ValueError, match="non-negative integers"):
        OperationReconciliationReport(examined_count=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exceed examined"):
        OperationReconciliationReport(
            examined_count=1,
            released_execution_count=2,
        )


@pytest.mark.asyncio
async def test_worker_sweeps_at_startup_then_periodically_and_stops_cleanly() -> None:
    reconciler = RecordingReconciler(
        [
            OperationReconciliationReport(
                examined_count=1,
                released_execution_count=1,
            ),
            OperationReconciliationReport.empty(),
        ]
    )
    worker = OperationReconciliationWorker(
        reconciler,
        interval=timedelta(milliseconds=1),
        batch_size=17,
    )

    async with worker:
        assert worker.is_running
        assert reconciler.limits == [17]
        reconciler.called.clear()
        await asyncio.wait_for(reconciler.called.wait(), timeout=1)

    calls_after_close = len(reconciler.limits)
    assert not worker.is_running
    await asyncio.sleep(0.01)
    assert len(reconciler.limits) == calls_after_close == 2
    await worker.aclose()


@pytest.mark.asyncio
async def test_worker_serializes_overlapping_manual_sweeps() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    class BlockingReconciler:
        async def reconcile(
            self,
            *,
            limit: int = 100,
        ) -> OperationReconciliationReport:
            nonlocal active, calls, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            active -= 1
            return OperationReconciliationReport.empty()

    worker = OperationReconciliationWorker(BlockingReconciler())
    first = asyncio.create_task(worker.sweep())
    await first_started.wait()
    second = asyncio.create_task(worker.sweep())
    await asyncio.sleep(0)
    assert calls == 1
    release_first.set()

    assert await asyncio.gather(first, second) == [
        OperationReconciliationReport.empty(),
        OperationReconciliationReport.empty(),
    ]
    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_worker_reports_failure_without_logging_exception_content() -> None:
    error = RuntimeError("private operation detail")
    reconciler = RecordingReconciler([error])
    worker = OperationReconciliationWorker(reconciler)

    with patch("derp.operations.reconciliation.report_exception") as report:
        result = await worker.sweep()

    assert result == OperationReconciliationReport.empty()
    report.assert_called_once_with(
        "operation_reconciliation_sweep_failed",
        exception=error,
        level="warning",
    )


@pytest.mark.asyncio
async def test_worker_surfaces_incomplete_results_as_content_free_count() -> None:
    result = OperationReconciliationReport(
        examined_count=2,
        incomplete_result_count=2,
    )
    worker = OperationReconciliationWorker(
        RecordingReconciler([result]),
    )

    with patch("derp.operations.reconciliation.logfire.warning") as warning:
        assert await worker.sweep() == result

    warning.assert_called_once_with(
        "paid_operation_result_incomplete",
        incomplete_result_count=2,
    )


@pytest.mark.asyncio
async def test_worker_rejects_double_start() -> None:
    worker = OperationReconciliationWorker(
        RecordingReconciler([OperationReconciliationReport.empty()]),
    )

    await worker.__aenter__()
    with pytest.raises(RuntimeError, match="already running"):
        await worker.__aenter__()
    await worker.aclose()
