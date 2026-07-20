"""Lifecycle-owned expiration worker for subscription allowance cycles."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from types import TracebackType
from typing import Protocol, Self

import logfire

from derp.observability import report_exception

DEFAULT_SUBSCRIPTION_EXPIRY_INTERVAL = timedelta(minutes=5)
MAX_SUBSCRIPTION_EXPIRY_INTERVAL = timedelta(days=1)


class SubscriptionCycleExpirer(Protocol):
    """Expire a bounded batch of due allowance cycles."""

    async def expire_due_cycles(self, *, limit: int = 100) -> int: ...


class SubscriptionExpiryWorker:
    """Periodically expire due cycles with graceful event-driven shutdown."""

    def __init__(
        self,
        expirer: SubscriptionCycleExpirer,
        *,
        interval: timedelta = DEFAULT_SUBSCRIPTION_EXPIRY_INTERVAL,
        batch_size: int = 100,
    ) -> None:
        if interval <= timedelta(0) or interval > MAX_SUBSCRIPTION_EXPIRY_INTERVAL:
            raise ValueError("Expiry interval must be positive and at most one day")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._expirer = expirer
        self._interval_seconds = interval.total_seconds()
        self._batch_size = batch_size
        self._sweep_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the periodic task is currently owned by this worker."""
        return self._task is not None

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("Subscription expiry worker is already running")
        self._stop.clear()
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="subscription-expiry",
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
        """Expire one serialized batch without exposing payer or payment data."""
        async with self._sweep_lock:
            try:
                expired = await self._expirer.expire_due_cycles(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "subscription_expiry_sweep_failed",
                    exception=exc,
                    level="warning",
                )
                return 0
        if expired:
            logfire.info(
                "subscription_cycles_expired",
                expired_cycle_count=expired,
            )
        return expired

    async def aclose(self) -> None:
        """Signal, join, and release the periodic task before DB shutdown."""
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
    "DEFAULT_SUBSCRIPTION_EXPIRY_INTERVAL",
    "MAX_SUBSCRIPTION_EXPIRY_INTERVAL",
    "SubscriptionCycleExpirer",
    "SubscriptionExpiryWorker",
]
