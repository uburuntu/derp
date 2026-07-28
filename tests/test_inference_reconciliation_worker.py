"""Lifecycle contracts for OpenRouter cost reconciliation."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from derp.inference import (
    OpenRouterCostReconciliationReport,
    OpenRouterCostReconciliationWorker,
)


class RecordingReconciler:
    def __init__(
        self,
        results: list[OpenRouterCostReconciliationReport | BaseException],
    ) -> None:
        self._results = iter(results)
        self.limits: list[int] = []
        self.called = asyncio.Event()

    async def reconcile(
        self,
        *,
        limit: int = 20,
    ) -> OpenRouterCostReconciliationReport:
        self.limits.append(limit)
        result = next(self._results)
        self.called.set()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize(
    ("interval", "batch_size"),
    [
        (timedelta(0), 20),
        (timedelta(days=1, microseconds=1), 20),
        (timedelta(minutes=5), False),
        (timedelta(minutes=5), 0),
        (timedelta(minutes=5), 101),
    ],
)
def test_worker_rejects_unbounded_configuration(
    interval: timedelta,
    batch_size: int,
) -> None:
    with pytest.raises(ValueError):
        OpenRouterCostReconciliationWorker(
            RecordingReconciler([]),
            interval=interval,
            batch_size=batch_size,
        )


@pytest.mark.asyncio
async def test_worker_sweeps_at_startup_then_periodically_and_stops() -> None:
    startup = OpenRouterCostReconciliationReport(1, 1, 0, 0, 0)
    periodic = OpenRouterCostReconciliationReport.empty()
    reconciler = RecordingReconciler([startup, periodic])
    worker = OpenRouterCostReconciliationWorker(
        reconciler,
        interval=timedelta(milliseconds=1),
        batch_size=17,
    )

    async with worker:
        assert worker.is_running
        await asyncio.wait_for(reconciler.called.wait(), timeout=1)
        assert reconciler.limits == [17]
        reconciler.called.clear()
        await asyncio.wait_for(reconciler.called.wait(), timeout=1)

    calls_after_close = len(reconciler.limits)
    assert not worker.is_running
    await asyncio.sleep(0.01)
    assert len(reconciler.limits) == calls_after_close == 2
    await worker.aclose()


@pytest.mark.asyncio
async def test_worker_serializes_overlapping_sweeps() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    maximum = 0
    calls = 0

    class BlockingReconciler:
        async def reconcile(
            self,
            *,
            limit: int = 20,
        ) -> OpenRouterCostReconciliationReport:
            nonlocal active, calls, maximum
            calls += 1
            active += 1
            maximum = max(maximum, active)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            active -= 1
            return OpenRouterCostReconciliationReport.empty()

    worker = OpenRouterCostReconciliationWorker(BlockingReconciler())
    first = asyncio.create_task(worker.sweep())
    await first_started.wait()
    second = asyncio.create_task(worker.sweep())
    await asyncio.sleep(0)
    assert calls == 1
    release_first.set()

    assert await asyncio.gather(first, second) == [
        OpenRouterCostReconciliationReport.empty(),
        OpenRouterCostReconciliationReport.empty(),
    ]
    assert calls == 2
    assert maximum == 1


@pytest.mark.asyncio
async def test_worker_recovers_from_startup_failure_without_exposing_details() -> None:
    error = RuntimeError("private provider detail")
    recovered = OpenRouterCostReconciliationReport(1, 0, 1, 0, 0)
    reconciler = RecordingReconciler([error, recovered])
    worker = OpenRouterCostReconciliationWorker(
        reconciler,
        interval=timedelta(milliseconds=1),
    )

    with patch("derp.inference.reconciliation.report_exception") as report:
        async with worker:
            await asyncio.wait_for(reconciler.called.wait(), timeout=1)
            reconciler.called.clear()
            await asyncio.wait_for(reconciler.called.wait(), timeout=1)

    report.assert_called_once_with(
        "openrouter_cost_reconciliation_sweep_failed",
        exception=error,
        level="warning",
    )
    assert reconciler.limits == [20, 20]


@pytest.mark.asyncio
async def test_worker_returns_empty_aggregate_after_sweep_failure() -> None:
    reconciler = RecordingReconciler([RuntimeError("private generation id")])
    worker = OpenRouterCostReconciliationWorker(reconciler, batch_size=7)

    with patch("derp.inference.reconciliation.report_exception"):
        result = await worker.sweep()

    assert result == OpenRouterCostReconciliationReport.empty()
    assert reconciler.limits == [7]
    assert not {
        "usage_id",
        "generation_id",
        "user_id",
        "chat_id",
    } & set(result.__dataclass_fields__)


@pytest.mark.asyncio
async def test_worker_propagates_sweep_cancellation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingReconciler:
        async def reconcile(
            self,
            *,
            limit: int = 20,
        ) -> OpenRouterCostReconciliationReport:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    worker = OpenRouterCostReconciliationWorker(BlockingReconciler())
    task = asyncio.create_task(worker.sweep())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_close_cancels_an_in_flight_periodic_sweep() -> None:
    periodic_started = asyncio.Event()
    periodic_cancelled = asyncio.Event()

    class BlockingPeriodicReconciler:
        async def reconcile(
            self,
            *,
            limit: int = 20,
        ) -> OpenRouterCostReconciliationReport:
            periodic_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                periodic_cancelled.set()
                raise

    worker = OpenRouterCostReconciliationWorker(
        BlockingPeriodicReconciler(),
        interval=timedelta(milliseconds=1),
    )
    await worker.__aenter__()
    await asyncio.wait_for(periodic_started.wait(), timeout=1)
    await asyncio.wait_for(worker.aclose(), timeout=1)

    assert periodic_cancelled.is_set()
    assert not worker.is_running


@pytest.mark.asyncio
async def test_startup_does_not_wait_for_the_external_sweep() -> None:
    started = asyncio.Event()

    class BlockingReconciler:
        async def reconcile(
            self,
            *,
            limit: int = 20,
        ) -> OpenRouterCostReconciliationReport:
            started.set()
            await asyncio.Event().wait()

    worker = OpenRouterCostReconciliationWorker(BlockingReconciler())

    await asyncio.wait_for(worker.__aenter__(), timeout=0.1)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(worker.aclose(), timeout=1)
