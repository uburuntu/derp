"""Runtime readiness reflects event-loop liveness without a network server."""

from __future__ import annotations

import asyncio
import os

import pytest

from derp.health import RuntimeHeartbeat, heartbeat_is_fresh
from derp.healthcheck import main


async def test_runtime_heartbeat_refreshes_and_cleans_up(tmp_path) -> None:
    path = tmp_path / "private" / "ready"

    async with RuntimeHeartbeat(path, interval_seconds=0.01):
        first = float(path.read_text())
        await asyncio.sleep(0.03)
        second = float(path.read_text())

        assert second > first
        assert heartbeat_is_fresh(path, now=second + 1)
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700

    assert not path.exists()


@pytest.mark.parametrize("content", ["", "not-a-time", "nan", "inf"])
def test_invalid_or_non_fresh_heartbeat_is_unhealthy(tmp_path, content) -> None:
    path = tmp_path / "ready"
    path.write_text(content)

    assert not heartbeat_is_fresh(path, now=100)


def test_healthcheck_reads_environment_without_importing_settings(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ready"
    path.write_text("100\n")
    monkeypatch.setenv("RUNTIME_HEALTH_PATH", os.fspath(path))
    monkeypatch.setenv("RUNTIME_HEALTH_MAX_AGE_SECONDS", "10")

    with pytest.MonkeyPatch.context() as clock:
        clock.setattr("derp.health.time.time", lambda: 105)
        assert main() == 0
        clock.setattr("derp.health.time.time", lambda: 111)
        assert main() == 1


def test_healthcheck_rejects_invalid_max_age(monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_HEALTH_MAX_AGE_SECONDS", "invalid")

    assert main() == 1
