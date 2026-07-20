"""Explicit task-local policy for durable outbound conversation history."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_capture_outbound: ContextVar[bool] = ContextVar(
    "capture_outbound_history",
    default=False,
)


def should_capture_outbound() -> bool:
    """Return whether Bot API message results belong to the active conversation."""
    return _capture_outbound.get()


@contextmanager
def capture_outbound_history() -> Iterator[None]:
    """Mark Bot API messages emitted in this task as conversational output."""
    token = _capture_outbound.set(True)
    try:
        yield
    finally:
        _capture_outbound.reset(token)


__all__ = ["capture_outbound_history", "should_capture_outbound"]
