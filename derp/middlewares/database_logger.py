"""Aiogram middleware for logging updates to Gel database."""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import logfire
from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import TelegramObject, Update

from ..common.database import DatabaseClient
from ..common.message_log import upsert_message_from_update
from ..common.tg import decompose_update
from ..common.update_context import UpdateContext, update_ctx


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

        # Insert BotUpdate immediately so Chat/User exist for projections
        bot_update_id: UUID = await self.db.create_bot_update_with_upserts(
            update_id=event.update_id,
            update_type=update_type,
            raw_data=raw_data,
            user=user,
            chat=chat,
            sender_chat=sender_chat,
            handled=False,
        )

        # Expose correlation context to downstream (e.g., API session middleware)
        token = update_ctx.set(
            UpdateContext(
                update_id=event.update_id,
                chat_id=chat and chat.id,
                user_id=user and user.id,
                thread_id=event.message and event.message.message_thread_id,
            )
        )

        # Project inbound message to MessageLog BEFORE handler to allow context reads
        try:
            async with self.db.get_executor() as executor:
                await upsert_message_from_update(executor, update=event, direction="in")
        except Exception as exc:
            logfire.warning("persist_inbound_failed", _exc_info=exc)

        # Execute the handler with Logfire baggage for correlation across spans/logs
        try:
            baggage: dict[str, str] = {"update_id": str(event.update_id)}
            if chat and chat.id is not None:
                baggage["chat_id"] = str(chat.id)
            if user and user.id is not None:
                baggage["user_id"] = str(user.id)

            with logfire.set_baggage(**baggage):
                response = await handler(event, data)
        finally:
            # Clear context to avoid leaks to unrelated tasks
            update_ctx.reset(token)

        # Update the handled status based on whether the response was handled
        if response is not UNHANDLED:
            await self.db.update_bot_update_handled_status(
                bot_update_id=bot_update_id,
                handled=True,
            )

        return response
