"""Aiogram middleware for logging updates to PostgreSQL database."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from derp.common.message_log import upsert_message_from_update
from derp.common.tg import decompose_update
from derp.db import DatabaseManager, upsert_chat, upsert_user
from derp.observability import report_exception


class DatabaseLoggerMiddleware(BaseMiddleware):
    """Middleware that logs incoming Telegram updates to the database.

    Upserts user and chat records, then projects the message to the
    messages table for LLM context building.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            raise RuntimeError("Got an unexpected event type")

        update_type = event.event_type

        # Skip inline queries (no message context)
        if update_type == "inline_query":
            return await handler(event, data)

        _, user, sender_chat, chat, _ = decompose_update(event)

        # Upsert user and chat records
        async with self.db.session() as session:
            if user:
                await upsert_user(
                    session,
                    telegram_id=user.id,
                    is_bot=user.is_bot,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    username=user.username,
                    language_code=user.language_code,
                    is_premium=user.is_premium or False,
                )

            if chat:
                await upsert_chat(
                    session,
                    telegram_id=chat.id,
                    chat_type=chat.type,
                    title=chat.title,
                    username=chat.username,
                    first_name=chat.first_name,
                    last_name=chat.last_name,
                    is_forum=chat.is_forum or False,
                )

            # Also upsert sender_chat if different from chat (e.g., channel posts)
            if sender_chat and (not chat or sender_chat.id != chat.id):
                await upsert_chat(
                    session,
                    telegram_id=sender_chat.id,
                    chat_type=sender_chat.type,
                    title=sender_chat.title,
                    username=sender_chat.username,
                    first_name=sender_chat.first_name,
                    last_name=sender_chat.last_name,
                    is_forum=sender_chat.is_forum or False,
                )

        # Project inbound message to messages table BEFORE handler for context reads
        try:
            await upsert_message_from_update(self.db, update=event, direction="in")
        except Exception as exc:
            report_exception(
                "persist_inbound_failed",
                exception=exc,
                level="warning",
            )

        return await handler(event, data)
