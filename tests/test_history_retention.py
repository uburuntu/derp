"""Tests for lifecycle-owned conversation history expiration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest

from derp.history.retention import HistoryRetentionWorker


class FakeDatabase:
    def __init__(self) -> None:
        self.session_entries = 0

    @asynccontextmanager
    async def session(self):
        self.session_entries += 1
        yield object()


class ControlledSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.release.clear()
            self.waiting.clear()


@pytest.mark.parametrize(
    "interval",
    [timedelta(seconds=59), timedelta(days=1, seconds=1)],
)
def test_retention_worker_rejects_unsafe_intervals(interval: timedelta) -> None:
    with pytest.raises(ValueError, match="between one minute and one day"):
        HistoryRetentionWorker(FakeDatabase(), interval=interval)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_worker_sweeps_immediately_then_periodically_and_joins() -> None:
    database = FakeDatabase()
    sleeper = ControlledSleep()
    periodic_swept = asyncio.Event()
    calls = 0

    async def purge_expired(_session: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            periodic_swept.set()
        return calls

    worker = HistoryRetentionWorker(
        database,  # type: ignore[arg-type]
        interval=timedelta(minutes=5),
        sleep=sleeper,
    )
    assert not worker.is_running
    with patch(
        "derp.history.retention.purge_expired_history",
        side_effect=purge_expired,
    ):
        async with worker:
            assert worker.is_running
            assert calls == 1
            await sleeper.waiting.wait()
            assert sleeper.delays == [300]
            sleeper.release.set()
            await periodic_swept.wait()
            await sleeper.waiting.wait()

    assert calls == 2
    assert not worker.is_running
    assert database.session_entries == 2
    assert sleeper.cancelled.is_set()


@pytest.mark.asyncio
async def test_worker_serializes_concurrent_sweeps() -> None:
    database = FakeDatabase()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    async def purge_expired(_session: object) -> int:
        nonlocal active, calls, max_active
        calls += 1
        active += 1
        max_active = max(max_active, active)
        if calls == 1:
            first_started.set()
            await release_first.wait()
        active -= 1
        return 1

    worker = HistoryRetentionWorker(database)  # type: ignore[arg-type]
    with patch(
        "derp.history.retention.purge_expired_history",
        side_effect=purge_expired,
    ):
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
    database = FakeDatabase()
    sleeper = ControlledSleep()
    periodic_swept = asyncio.Event()
    error = RuntimeError("private database detail")
    calls = 0

    async def purge_expired(_session: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        periodic_swept.set()
        return 4

    worker = HistoryRetentionWorker(
        database,  # type: ignore[arg-type]
        sleep=sleeper,
    )
    with (
        patch(
            "derp.history.retention.purge_expired_history",
            side_effect=purge_expired,
        ),
        patch("derp.history.retention.report_exception") as report,
    ):
        async with worker:
            report.assert_called_once_with(
                "history_retention_sweep_failed",
                exception=error,
                level="warning",
            )
            await sleeper.waiting.wait()
            sleeper.release.set()
            await periodic_swept.wait()

    assert calls == 2
