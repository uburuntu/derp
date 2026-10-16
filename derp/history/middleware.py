"""Capture every message before handlers run, and tell handlers whether Derp was invoked."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from derp.agent.invocation import detect_trigger
from derp.db.engine import Database
from derp.history.capture import index_sticker, store_message, upsert_chat, upsert_user
from derp.history.members import track_message
from derp.history.transcription import Transcriber


class CaptureMiddleware(BaseMiddleware):
    """Stores invocations always, ambient messages when the chat allows it, and edits in place.

    Stored voice messages and video notes are transcribed before handlers run.

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
            stored = False
            if trigger is not None or chat.type == "private" or chat.ambient:
                stored = await store_message(session, event, capture="invocation" if trigger else "ambient")
            if not self.edits:
                await track_message(session, event, stored=stored)
                await index_sticker(session, event)
        transcriber: Transcriber | None = data.get("transcriber")
        if stored and not self.edits and transcriber is not None and (event.voice or event.video_note):
            await transcriber.transcribe_message(event.chat.id, event.message_id)
        data["trigger"] = trigger
        data["chat_row"] = chat
        return await handler(event, data)
