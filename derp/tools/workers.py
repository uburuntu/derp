"""The local CPU worker pool: a few slots, a short queue, killable processes.

Every media effect takes a slot first; when all slots are busy and the queue
is full the caller is told to try later instead of piling up. Native work
(ffmpeg, the background model) runs as a subprocess with its own deadline and
an output cap and is killed when either runs out. Diagnostics from native
tools can echo file contents or paths, so they are discarded.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence

SLOTS = 2
QUEUE = 4
MAX_INPUT_BYTES = 20 * 1024 * 1024  # what the Bot API lets a bot download
MAX_OUTPUT_BYTES = 50 * 1024 * 1024  # what the Bot API lets a bot upload
MAX_PIXELS = 16_000_000
MAX_SIDE = 8192
TIMEOUT = 60


class EffectError(Exception):
    """A media effect failed; ``reason`` is safe to show (no paths, text or native output)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BusyError(EffectError):
    def __init__(self) -> None:
        super().__init__("all media workers are busy")


def check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
        raise EffectError("too large (16 MP and 8192 px per side at most)")


class WorkerPool:
    def __init__(self, slots: int = SLOTS, queue: int = QUEUE) -> None:
        self._slots = asyncio.Semaphore(slots)
        self._limit = slots + queue
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._waiting >= self._limit:
            raise BusyError
        self._waiting += 1
        try:
            async with self._slots:
                yield
        finally:
            self._waiting -= 1


async def run(
    args: Sequence[str], *, seconds: float = TIMEOUT, max_output: int = MAX_OUTPUT_BYTES, what: str = "effect"
) -> bytes:
    """Run a native worker and return its stdout; kills it past ``seconds`` or ``max_output``."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout = process.stdout
    chunks: list[bytes] = []
    size = 0

    async def drain() -> None:
        nonlocal size
        while stdout is not None and (chunk := await stdout.read(1 << 16)):
            size += len(chunk)
            if size > max_output:
                raise EffectError(f"{what} output too large")
            chunks.append(chunk)
        await process.wait()

    try:
        async with asyncio.timeout(seconds):
            await drain()
    except TimeoutError:
        raise EffectError(f"{what} took too long") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise EffectError(f"{what} failed")
    return b"".join(chunks)


POOL = WorkerPool()
