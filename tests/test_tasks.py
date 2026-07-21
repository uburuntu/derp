"""Background task health distinguishes live, completed, and failed work."""

import asyncio

import pytest

from derp.common.tasks import task_is_running


async def test_task_health_reports_only_pending_tasks_as_running() -> None:
    release = asyncio.Event()
    pending = asyncio.create_task(release.wait())
    completed = asyncio.create_task(asyncio.sleep(0))

    assert task_is_running(pending)
    await completed
    assert not task_is_running(completed)
    assert not task_is_running(None)

    release.set()
    await pending


async def test_task_health_reports_failed_tasks_as_stopped() -> None:
    async def fail() -> None:
        raise RuntimeError("worker failed")

    failed = asyncio.create_task(fail())
    with pytest.raises(RuntimeError, match="worker failed"):
        await failed

    assert not task_is_running(failed)
