import asyncio
from collections.abc import Callable
from concurrent.futures.process import (
    BrokenProcessPool,
)
from concurrent.futures.process import (
    ProcessPoolExecutor as _ProcessPoolExecutor,
)
from concurrent.futures.thread import (
    BrokenThreadPool,
)
from concurrent.futures.thread import (
    ThreadPoolExecutor as _ThreadPoolExecutor,
)
from typing import Any

from throttler import ThrottlerSimultaneous


class _BaseExecutor:
    ExecutorClass = None
    ExecutorExceptionClass = None

    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self.throttler = ThrottlerSimultaneous(count=max_workers)

        self._executor = None  # Lazy initialization

    @property
    def executor(self):
        if self._executor:
            return self._executor
        self._executor = self.ExecutorClass(max_workers=self.max_workers)
        return self._executor

    async def run(
        self, func: Callable, *args, timeout: float | None = 180
    ) -> tuple[Any, bool]:
        async with self.throttler:
            try:
                future = asyncio.get_running_loop().run_in_executor(
                    self.executor, func, *args
                )
                try:
                    result = await asyncio.wait_for(future, timeout=timeout)
                except asyncio.exceptions.TimeoutError:
                    return None, True
                return result, False
            except self.ExecutorExceptionClass:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
                return await self.run(func, *args, timeout)

    def shutdown(self, wait: bool):
        return self.executor.shutdown(wait=wait, cancel_futures=True)


class ThreadPoolExecutor(_BaseExecutor):
    ExecutorClass = _ThreadPoolExecutor
    ExecutorExceptionClass = BrokenThreadPool


class ProcessPoolExecutor(_BaseExecutor):
    ExecutorClass = _ProcessPoolExecutor
    ExecutorExceptionClass = BrokenProcessPool
