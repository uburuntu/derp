"""Middleware for loading and injecting database models."""

from collections.abc import Awaitable, Callable
from typing import Any

import logfire
from aiogram import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import (
    EVENT_CHAT_KEY,
    EVENT_FROM_USER_KEY,
)
from aiogram.types import Chat, TelegramObject, User

from derp.db import DatabaseManager, get_chat_settings
from derp.db.queries import get_user_by_telegram_id
from derp.models import Chat as ChatModel
from derp.models import User as UserModel


class DatabaseModelMiddleware(BaseMiddleware):
    """Middleware that loads and injects database models into handler data.

    Adds 'user_model' and 'chat_model' keys containing the SQLAlchemy models
    (UserModel and ChatModel) for database operations and credit management.
    This avoids repeated lookups in handlers.

    Usage in handlers:
        async def my_handler(
            message: Message,
            user_model: UserModel | None = None,
            chat_model: ChatModel | None = None,
        ):
            if user_model:
                print(user_model.id)  # UUID
                print(user_model.credits)  # Credit balance
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get(EVENT_FROM_USER_KEY)
        chat: Chat | None = data.get(EVENT_CHAT_KEY)

        try:
            async with self.db.read_session() as session:
                if user:
                    user_model: UserModel | None = await get_user_by_telegram_id(
                        session, telegram_id=user.id
                    )
                    data["user_model"] = user_model

                if chat:
                    chat_model: ChatModel | None = await get_chat_settings(
                        session, telegram_id=chat.id
                    )
                    data["chat_model"] = chat_model

        except Exception:
            logfire.exception(
                "db_model_load_failed",
                user_id=user.id if user else None,
                chat_id=chat.id if chat else None,
            )

        return await handler(event, data)
