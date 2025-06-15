"""Aiogram middleware for logging updates to Gel database."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import TelegramObject, Update

from ..common.database import DatabaseClient
from ..common.tg import decompose_update


class DatabaseLoggerMiddleware(BaseMiddleware):
    def __init__(self, db: DatabaseClient):
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

        if update_type == "inline_query":
            return await handler(event, data)

        raw_data = event.model_dump(
            exclude_unset=True, exclude_defaults=True, exclude_none=True
        )
        _, user, sender_chat, chat, _ = decompose_update(event)

        # Start the database operation but don't block the request
        insert_task = asyncio.create_task(
            self.db.create_bot_update_with_upserts(
                update_id=event.update_id,
                update_type=update_type,
                raw_data=raw_data,
                user=user,
                chat=chat,
                sender_chat=sender_chat,
                handled=False,  # Initially mark as not handled
            )
        )

        # Put the task in data so handlers can await it if needed
        data["db_task"] = insert_task

        # Execute the handler (without blocking on database)
        response = await handler(event, data)

        # Now await the database operation completion
        bot_update_id: UUID = await insert_task

        # Update the handled status based on whether the response was handled
        if response is not UNHANDLED:
            await self.db.update_bot_update_handled_status(
                bot_update_id=bot_update_id,
                handled=True,
            )

        return response
