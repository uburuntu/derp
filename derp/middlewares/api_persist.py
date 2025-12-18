"""Session middleware to persist outbound Bot API actions into messages table.

Captures results of send/edit/delete operations and writes a cleaned record.
It correlates with the current inbound update via contextvars.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import logfire
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import TelegramMethod
from aiogram.methods.delete_message import DeleteMessage
from aiogram.types import Message

from derp.common.message_log import mark_deleted, upsert_message_from_message
from derp.common.update_context import update_ctx
from derp.db import DatabaseManager


class PersistBotActionsMiddleware(BaseRequestMiddleware):
    """Middleware that persists bot actions (send/edit/delete) to the database.

    Intercepts Bot API responses and saves message data for LLM context.
    """

    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db

    async def __call__(
        self,
        make_request,
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        # Perform request first
        result = await make_request(bot, method)

        # Best-effort persist only when in a correlated update context
        ctx = update_ctx.get()
        if not ctx:
            return result

        try:
            # Handle messages (single or list)
            messages: list[Message] = []
            if isinstance(result, Message):
                messages = [result]
            elif isinstance(result, Iterable):
                messages = [m for m in result if isinstance(m, Message)]

            for m in messages:
                await upsert_message_from_message(
                    self.db,
                    message=m,
                    direction="out",
                )

            # Handle deletes: when deleteMessage returns bool
            if (
                isinstance(method, DeleteMessage)
                and isinstance(result, bool)
                and result
            ):
                await mark_deleted(
                    self.db,
                    chat_id=int(method.chat_id),
                    message_id=int(method.message_id),
                )
        except Exception:
            # Never break outbound calls due to persistence issues
            logfire.exception("persist_outbound_failed")

        return result
