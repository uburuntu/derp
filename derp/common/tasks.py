"""Shared lifecycle predicates for background asyncio tasks."""

from __future__ import annotations

import asyncio


def task_is_running[T](task: asyncio.Future[T] | None) -> bool:
    """Return whether a task exists and has not completed or failed."""
    return task is not None and not task.done()


__all__ = ["task_is_running"]
