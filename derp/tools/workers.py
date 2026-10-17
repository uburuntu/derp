"""The local CPU worker pool: a few slots, a short queue, killable processes.

Every media effect takes a slot first; when all slots are busy and the queue
is full the caller is told to try later instead of piling up. The pool and its
limits (input and output size, pixels, deadline) come from ``settings.media``;
``derp.app`` builds the one pool, nothing is created at import. Native work
(ffmpeg, the background model) runs as a subprocess with its own deadline and
an output cap and is killed when either runs out. Diagnostics from native
tools can echo file contents or paths, so they are discarded.
"""

import asyncio
import contextlib
import resource
from collections.abc import AsyncIterator, Sequence

from derp.config import MediaLimits

DEFAULTS = MediaLimits()  # for tests and callers outside the app (the app passes settings.media)
MAX_SIDE = 8192

MAX_WORKER_MEMORY = 2048 * 1024 * 1024  # address space per native worker


class EffectError(Exception):
    """A media effect failed; ``reason`` is safe to show (no paths, text or native output)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BusyError(EffectError):
    def __init__(self) -> None:
        super().__init__("all media workers are busy")


def check_dimensions(width: int, height: int, max_pixels: int = DEFAULTS.max_pixels) -> None:
    if width <= 0 or height <= 0 or max(width, height) > MAX_SIDE or width * height > max_pixels:
        raise EffectError(f"too large ({max_pixels / 1e6:g} MP and {MAX_SIDE} px per side at most)")


class WorkerPool:
    def __init__(self, limits: MediaLimits = DEFAULTS) -> None:
        self.limits = limits
        self._slots = asyncio.Semaphore(limits.workers)
        self._limit = limits.workers + limits.queue
        self._waiting = 0

    def check_input(self, data: bytes) -> None:
        if len(data) > self.limits.max_input_bytes:
            raise EffectError(f"the file is too large ({self.limits.max_input_bytes // 2**20} MB at most)")

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


def _cap_memory() -> None:
    """A crafted file can't make a worker eat the host's memory (best effort where unsupported)."""
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_AS, (MAX_WORKER_MEMORY, MAX_WORKER_MEMORY))


async def run(args: Sequence[str], *, seconds: float, max_output: int, what: str = "effect") -> bytes:
    """Run a native worker and return its stdout; kills it past ``seconds`` or ``max_output``."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=_cap_memory,
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
