"""Capture every message before handlers run, and tell handlers whether Derp was invoked."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from derp.agent.invocation import detect_trigger
from derp.db.engine import Database
from derp.history.capture import store_message, upsert_chat, upsert_user


class CaptureMiddleware(BaseMiddleware):
    """Stores invocations always, ambient messages when the chat allows it, and edits in place.

    Adds ``trigger`` (or None) and ``chat_row`` to handler data. Guest-mode
    messages pass through untouched: they are never stored.
    """

    def __init__(self, *, edits: bool = False) -> None:
        self.edits = edits

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.guest_query_id:
            return await handler(event, data)
        db: Database = data["db"]
        bot: Bot = data["bot"]
        me = await bot.me()
        trigger = None if self.edits else detect_trigger(event, bot_id=me.id, bot_username=me.username or "")
        async with db.transaction() as session:
            chat = await upsert_chat(session, event.chat)
            if event.from_user is not None:
                await upsert_user(session, event.from_user)
            if trigger is not None or chat.type == "private" or chat.ambient:
                await store_message(session, event, capture="invocation" if trigger else "ambient")
        data["trigger"] = trigger
        data["chat_row"] = chat
        return await handler(event, data)
