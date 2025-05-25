"""Aiogram middleware for logging updates to Gel database."""

from collections.abc import Awaitable, Callable
from functools import cached_property
from typing import Any

import aiojobs
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from ..common.database import get_database_client
from ..common.tg import decompose_update


class DatabaseLoggerMiddleware(BaseMiddleware):
    def __init__(self):
        self.db_client = get_database_client()

    @cached_property
    def scheduler(self) -> aiojobs.Scheduler:
        """Get the scheduler instance."""
        return aiojobs.Scheduler(
            close_timeout=0.3, limit=100, pending_limit=10_000, exception_handler=None
        )

    async def close(self) -> None:
        """Close the scheduler and cleanup resources."""
        await self.scheduler.close()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            raise RuntimeError("Got an unexpected event type")

        update_type = event.event_type
        raw_data = event.model_dump(
            exclude_unset=True, exclude_defaults=True, exclude_none=True
        )
        _, user, sender_chat, chat, _ = decompose_update(event)

        user_id = user and user.id
        chat_id = chat and chat.id

        if user:
            coro = self.db_client.upsert_user_record(user)
            await self.scheduler.spawn(coro)

        if chat:
            coro = self.db_client.upsert_chat_record(chat)
            await self.scheduler.spawn(coro)

        if sender_chat:
            coro = self.db_client.upsert_chat_record(sender_chat)
            await self.scheduler.spawn(coro)

        coro = self.db_client.insert_bot_update_record(
            update_id=event.update_id,
            update_type=update_type,
            raw_data=raw_data,
            user_id=user_id,
            chat_id=chat_id,
            handled=False,
        )
        await self.scheduler.spawn(coro)

        return await handler(event, data)
