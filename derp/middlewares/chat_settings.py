"""Middleware for loading and injecting chat settings."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import logfire
from aiogram import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY
from aiogram.types import Chat, TelegramObject

from derp.db import DatabaseManager, get_chat_settings
from derp.models import Chat as ChatModel


class ChatSettingsMiddleware(BaseMiddleware):
    """Middleware that loads and injects chat settings into handler data.

    Only processes events that have a chat context.
    Adds 'chat_settings' key to handler data containing the Chat model
    with llm_memory field.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.logger = logging.getLogger(__name__)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat: Chat | None = data.get(EVENT_CHAT_KEY)

        if chat is None:
            return await handler(event, data)

        try:
            async with self.db.session() as session:
                chat_model: ChatModel | None = await get_chat_settings(
                    session, telegram_id=chat.id
                )
                data["chat_settings"] = chat_model

        except Exception:
            logfire.exception("chat_settings_load_failed", chat_id=chat.id)

        return await handler(event, data)
