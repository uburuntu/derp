import logging
from collections.abc import Awaitable, Callable
from typing import Any

import gel
import logfire
from aiogram import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY
from aiogram.types import Chat, TelegramObject

from derp.queries.chat_settings_async_edgeql import chat_settings


class ChatSettingsMiddleware(BaseMiddleware):
    """
    Middleware that loads and injects chat settings into handler data.

    Only processes events that have a chat context.
    Adds 'chat_settings' key to handler data containing ChatSettings instance.
    """

    def __init__(self, db: gel.AsyncIOExecutor):
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
            async with self.db.get_executor() as executor:
                data["chat_settings"] = await chat_settings(executor, chat_id=chat.id)

        except Exception:
            logfire.exception("chat_settings_load_failed", chat_id=chat.id)

        return await handler(event, data)
