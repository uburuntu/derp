"""Tests for process bootstrap failure handling."""

from __future__ import annotations

from unittest.mock import MagicMock

from derp.__main__ import _shutdown_observability


def test_shutdown_timeout_is_reported(capsys) -> None:
    observability = MagicMock()
    observability.shutdown.return_value = False

    _shutdown_observability(observability)

    assert "Timed out while shutting down telemetry" in capsys.readouterr().err


def test_shutdown_failure_does_not_escape(capsys) -> None:
    observability = MagicMock()
    observability.shutdown.side_effect = RuntimeError("exporter details")

    _shutdown_observability(observability)

    stderr = capsys.readouterr().err
    assert "Telemetry shutdown failed: RuntimeError" in stderr
    assert "exporter details" not in stderr
