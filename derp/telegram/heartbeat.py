"""A successful getUpdates proves liveness even when no messages arrive.

``python -m derp.telegram.heartbeat`` exits 0 when the last poll is fresh; the
container healthcheck and the deploy's post-cutover check use it.
"""

import os
import time
from contextlib import suppress
from pathlib import Path

MAX_AGE_SECONDS = 180


def heartbeat_path() -> Path:
    return Path(os.environ.get("HEARTBEAT_PATH") or "/tmp/derp.poll")  # noqa: S108 - same env as Settings


def mark_poll_success(path: Path | None = None) -> None:
    # Liveness reporting must never break polling; a stale file makes the healthcheck fail instead.
    with suppress(OSError):
        (path or heartbeat_path()).write_text(str(time.time()))


def ready(path: Path | None = None, max_age: float = MAX_AGE_SECONDS) -> bool:
    try:
        age = time.time() - float((path or heartbeat_path()).read_text())
    except OSError, ValueError:
        return False
    return 0 <= age <= max_age


if __name__ == "__main__":
    raise SystemExit(0 if ready() else 1)
