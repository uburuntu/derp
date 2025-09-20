"""Context for correlating outbound bot actions to inbound updates.

We use a contextvar set by the outer DatabaseLoggerMiddleware so that
session middleware (which intercepts Bot API calls) can link bot actions
to the originating Telegram update without handlers passing values around.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class UpdateContext:
    update_id: int
    chat_id: int | None
    user_id: int | None
    thread_id: int | None


update_ctx: ContextVar[UpdateContext | None] = ContextVar("update_ctx", default=None)
