"""Tests for lifecycle-owned subscription allowance expiration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from derp.billing.expiry import SubscriptionExpiryWorker


class RecordingExpirer:
    def __init__(self, results: list[int | BaseException]) -> None:
        self.results = iter(results)
        self.limits: list[int] = []
        self.called = asyncio.Event()

    async def expire_due_cycles(self, *, limit: int = 100) -> int:
        self.limits.append(limit)
        result = next(self.results)
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
    ],
)
def test_worker_rejects_invalid_bounds(
    interval: timedelta,
    batch_size: int,
) -> None:
    with pytest.raises(ValueError):
        SubscriptionExpiryWorker(
            RecordingExpirer([0]),
            interval=interval,
            batch_size=batch_size,
        )


@pytest.mark.asyncio
async def test_worker_sweeps_immediately_then_periodically_and_stops() -> None:
    expirer = RecordingExpirer([1, 2, 3])
    worker = SubscriptionExpiryWorker(
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


@pytest.mark.asyncio
async def test_worker_serializes_concurrent_sweeps() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    class BlockingExpirer:
        async def expire_due_cycles(self, *, limit: int = 100) -> int:
            nonlocal active, calls, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            active -= 1
            return 1

    worker = SubscriptionExpiryWorker(BlockingExpirer())
    first = asyncio.create_task(worker.sweep())
    await first_started.wait()
    second = asyncio.create_task(worker.sweep())
    await asyncio.sleep(0)
    assert calls == 1
    release_first.set()

    assert await asyncio.gather(first, second) == [1, 1]
    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_worker_reports_failure_safely_and_retries() -> None:
    error = RuntimeError("private payment detail")
    expirer = RecordingExpirer([error, 4])
    worker = SubscriptionExpiryWorker(
        expirer,
        interval=timedelta(milliseconds=1),
    )

    with patch("derp.billing.expiry.report_exception") as report:
        async with worker:
            report.assert_called_once_with(
                "subscription_expiry_sweep_failed",
                exception=error,
                level="warning",
            )
            expirer.called.clear()
            await asyncio.wait_for(expirer.called.wait(), timeout=1)

    assert len(expirer.limits) == 2


@pytest.mark.asyncio
async def test_worker_rejects_double_start_and_close_is_idempotent() -> None:
    expirer = RecordingExpirer([0])
    worker = SubscriptionExpiryWorker(expirer)

    await worker.__aenter__()
    with pytest.raises(RuntimeError, match="already running"):
        await worker.__aenter__()
    await worker.aclose()
    await worker.aclose()
