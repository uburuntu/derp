"""Docker health-check entry point for the runtime heartbeat."""

from __future__ import annotations

import os
from pathlib import Path

from derp.health import (
    DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    DEFAULT_RUNTIME_HEALTH_PATH,
    heartbeat_is_fresh,
)


def main() -> int:
    """Return a conventional process status without emitting sensitive output."""
    path = Path(os.environ.get("RUNTIME_HEALTH_PATH", DEFAULT_RUNTIME_HEALTH_PATH))
    raw_max_age = os.environ.get(
        "RUNTIME_HEALTH_MAX_AGE_SECONDS",
        str(DEFAULT_HEARTBEAT_MAX_AGE_SECONDS),
    )
    try:
        max_age = float(raw_max_age)
    except ValueError:
        return 1
    return 0 if heartbeat_is_fresh(path, max_age_seconds=max_age) else 1


if __name__ == "__main__":
    raise SystemExit(main())
