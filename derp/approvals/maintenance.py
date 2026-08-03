"""Lifecycle-owned expiration for abandoned deferred tool approvals."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from types import TracebackType
from typing import Final, Protocol, Self

import logfire

from derp.approvals.types import ExpirationSweep
from derp.common.tasks import task_is_running
from derp.observability import report_exception

DEFAULT_APPROVAL_EXPIRY_INTERVAL: Final = timedelta(minutes=5)
MAX_APPROVAL_EXPIRY_INTERVAL: Final = timedelta(days=1)
MAX_APPROVAL_EXPIRY_BATCH_SIZE: Final = 1_000


class DeferredApprovalExpirer(Protocol):
    """Bounded payload-scrubbing transition owned by the approval service."""

    async def expire_stale(self, *, limit: int = 100) -> ExpirationSweep: ...


class DeferredApprovalExpiryWorker:
    """Expire abandoned approval state at startup and on a bounded cadence."""

    def __init__(
        self,
        expirer: DeferredApprovalExpirer,
        *,
        interval: timedelta = DEFAULT_APPROVAL_EXPIRY_INTERVAL,
        batch_size: int = 100,
    ) -> None:
        if interval <= timedelta(0) or interval > MAX_APPROVAL_EXPIRY_INTERVAL:
            raise ValueError("interval must be positive and at most one day")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= MAX_APPROVAL_EXPIRY_BATCH_SIZE
        ):
            raise ValueError(
                f"batch_size must be between 1 and {MAX_APPROVAL_EXPIRY_BATCH_SIZE}"
            )
        self._expirer = expirer
        self._interval_seconds = interval.total_seconds()
        self._batch_size = batch_size
        self._sweep_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the periodic task is alive rather than completed or failed."""
        return task_is_running(self._task)

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("Deferred approval expiry worker is already running")
        self._stop.clear()
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="deferred-approval-expiry",
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
        """Expire one serialized batch and expose only its aggregate count."""
        async with self._sweep_lock:
            try:
                result = await self._expirer.expire_stale(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "deferred_approval_expiry_sweep_failed",
                    exception=exc,
                    level="warning",
                )
                return 0
        if result.expired_count:
            logfire.info(
                "deferred_tool_approvals_expired",
                expired_request_count=result.expired_count,
            )
        return result.expired_count

    async def aclose(self) -> None:
        """Signal and join the worker before approval storage shuts down."""
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
    "DEFAULT_APPROVAL_EXPIRY_INTERVAL",
    "MAX_APPROVAL_EXPIRY_BATCH_SIZE",
    "MAX_APPROVAL_EXPIRY_INTERVAL",
    "DeferredApprovalExpirer",
    "DeferredApprovalExpiryWorker",
]
