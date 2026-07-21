"""Lifecycle-owned expiration worker for persisted conversation history."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from types import TracebackType
from typing import Self

from derp.db import DatabaseManager, purge_expired_history
from derp.observability import report_exception

DEFAULT_RETENTION_SWEEP_INTERVAL = timedelta(hours=1)
MIN_RETENTION_SWEEP_INTERVAL = timedelta(minutes=1)
MAX_RETENTION_SWEEP_INTERVAL = timedelta(days=1)

type Sleeper = Callable[[float], Awaitable[None]]


class HistoryRetentionWorker:
    """Periodically delete history whose disclosed retention period elapsed."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        interval: timedelta = DEFAULT_RETENTION_SWEEP_INTERVAL,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if not MIN_RETENTION_SWEEP_INTERVAL <= interval <= MAX_RETENTION_SWEEP_INTERVAL:
            raise ValueError(
                "Retention sweep interval must be between one minute and one day"
            )
        self._db = db
        self._interval_seconds = interval.total_seconds()
        self._sleep = sleep
        self._sweep_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether this worker owns its periodic lifecycle task."""
        return self._task is not None

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("History retention worker is already running")
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="history-retention",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> int:
        """Run one serialized expiration sweep without exposing row contents."""
        async with self._sweep_lock:
            try:
                async with self._db.session() as session:
                    return await purge_expired_history(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "history_retention_sweep_failed",
                    exception=exc,
                    level="warning",
                )
                return 0

    async def aclose(self) -> None:
        """Cancel and join the periodic task before its database is closed."""
        if (task := self._task) is None:
            return
        self._task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_periodically(self) -> None:
        while True:
            await self._sleep(self._interval_seconds)
            await self.sweep()
