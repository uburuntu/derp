"""Process-local event-loop readiness heartbeat."""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Self

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 45.0
DEFAULT_RUNTIME_HEALTH_PATH = Path(tempfile.gettempdir()) / "derp-runtime-ready"


class RuntimeHeartbeat:
    """Keep one private timestamp fresh while the application loop is responsive."""

    def __init__(
        self,
        path: str | Path,
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("heartbeat interval must be finite and positive")
        self._path = Path(path).expanduser().absolute()
        if not self._path.name:
            raise ValueError("heartbeat path must identify a file")
        self._interval_seconds = interval_seconds
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("heartbeat is already running")
        self._stopping.clear()
        await self.beat()
        self._task = asyncio.create_task(
            self._run(),
            name="runtime-heartbeat",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
        await asyncio.to_thread(self._remove)

    async def beat(self) -> None:
        """Atomically publish the current wall-clock time."""
        await asyncio.to_thread(self._write, time.time())

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                await self.beat()
            else:
                return

    def _write(self, timestamp: float) -> None:
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700, follow_symlinks=False)
        temporary = parent / f".{self._path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(f"{timestamp:.6f}\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove(self) -> None:
        self._path.unlink(missing_ok=True)


def heartbeat_is_fresh(
    path: str | Path,
    *,
    max_age_seconds: float = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    """Return whether a private heartbeat contains a recent finite timestamp."""
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("heartbeat max age must be finite and positive")
    try:
        raw = Path(path).read_text(encoding="ascii")
        timestamp = float(raw.strip())
    except OSError, UnicodeError, ValueError:
        return False
    current = time.time() if now is None else now
    age = current - timestamp
    return 0 <= age <= max_age_seconds


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_HEARTBEAT_MAX_AGE_SECONDS",
    "DEFAULT_RUNTIME_HEALTH_PATH",
    "RuntimeHeartbeat",
    "heartbeat_is_fresh",
]
