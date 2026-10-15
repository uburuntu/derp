"""Fire-and-forget periodic tasks owned by the application lifetime."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("derp.periodic")


async def every(
    name: str, seconds: float, job: Callable[[], Awaitable[object]], *, initial_delay: float = 60
) -> None:
    """Run ``job`` forever; failures are logged by type and never stop the loop."""
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one failed tick must not kill the schedule
            log.error("periodic %s failed: %s", name, type(error).__name__)
        await asyncio.sleep(seconds)
