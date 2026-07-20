"""Lifecycle and privacy contracts for deferred approval expiration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

from derp.approvals.maintenance import DeferredApprovalExpiryWorker
from derp.approvals.types import ExpirationSweep


class RecordingExpirer:
    def __init__(self, results: list[ExpirationSweep | BaseException]) -> None:
        self._results = iter(results)
        self.limits: list[int] = []
        self.called = asyncio.Event()

    async def expire_stale(self, *, limit: int = 100) -> ExpirationSweep:
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
        (timedelta(minutes=5), False),
        (timedelta(minutes=5), 0),
        (timedelta(minutes=5), 1_001),
    ],
)
def test_worker_rejects_unbounded_configuration(
    interval: timedelta,
    batch_size: int,
) -> None:
    with pytest.raises(ValueError):
        DeferredApprovalExpiryWorker(
            RecordingExpirer([]),
            interval=interval,
            batch_size=batch_size,
        )


@pytest.mark.asyncio
async def test_worker_sweeps_at_startup_then_periodically_and_stops_cleanly() -> None:
    expirer = RecordingExpirer(
        [
            ExpirationSweep((uuid.uuid4(),)),
            ExpirationSweep(()),
        ]
    )
    worker = DeferredApprovalExpiryWorker(
        expirer,
        interval=timedelta(milliseconds=1),
        batch_size=17,
    )

    async with worker:
        assert worker.is_running
        assert expirer.limits == [17]
        expirer.called.clear()
        await asyncio.wait_for(expirer.called.wait(), timeout=1)

    calls_after_close = len(expirer.limits)
    assert not worker.is_running
    await asyncio.sleep(0.01)
    assert len(expirer.limits) == calls_after_close == 2
    await worker.aclose()


@pytest.mark.asyncio
async def test_worker_serializes_overlapping_sweeps() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    class BlockingExpirer:
        async def expire_stale(self, *, limit: int = 100) -> ExpirationSweep:
            nonlocal active, calls, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            active -= 1
            return ExpirationSweep(())

    worker = DeferredApprovalExpiryWorker(BlockingExpirer())
    first = asyncio.create_task(worker.sweep())
    await first_started.wait()
    second = asyncio.create_task(worker.sweep())
    await asyncio.sleep(0)
    assert calls == 1
    release_first.set()

    assert await asyncio.gather(first, second) == [0, 0]
    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_worker_recovers_from_startup_failure_and_runs_future_sweeps() -> None:
    error = RuntimeError("private deferred arguments")
    expirer = RecordingExpirer([error, ExpirationSweep((uuid.uuid4(),))])
    worker = DeferredApprovalExpiryWorker(
        expirer,
        interval=timedelta(milliseconds=1),
    )

    with patch("derp.approvals.maintenance.report_exception") as report:
        async with worker:
            expirer.called.clear()
            await asyncio.wait_for(expirer.called.wait(), timeout=1)

    report.assert_called_once_with(
        "deferred_approval_expiry_sweep_failed",
        exception=error,
        level="warning",
    )
    assert len(expirer.limits) == 2


@pytest.mark.asyncio
async def test_worker_propagates_sweep_cancellation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingExpirer:
        async def expire_stale(self, *, limit: int = 100) -> ExpirationSweep:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    worker = DeferredApprovalExpiryWorker(BlockingExpirer())
    task = asyncio.create_task(worker.sweep())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_worker_reports_success_with_count_only() -> None:
    result = ExpirationSweep((uuid.uuid4(), uuid.uuid4()))
    worker = DeferredApprovalExpiryWorker(RecordingExpirer([result]))

    with patch("derp.approvals.maintenance.logfire.info") as info:
        assert await worker.sweep() == 2

    info.assert_called_once_with(
        "deferred_tool_approvals_expired",
        expired_request_count=2,
    )


@pytest.mark.asyncio
async def test_worker_rejects_double_start() -> None:
    worker = DeferredApprovalExpiryWorker(
        RecordingExpirer([ExpirationSweep(())]),
    )

    await worker.__aenter__()
    with pytest.raises(RuntimeError, match="already running"):
        await worker.__aenter__()
    await worker.aclose()
