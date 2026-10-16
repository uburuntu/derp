"""Capture every message before handlers run, and tell handlers whether Derp was invoked."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from derp.agent.invocation import detect_trigger, starts_with_command
from derp.db.engine import Database
from derp.history.capture import index_sticker, store_message, upsert_chat, upsert_user
from derp.history.members import track_message
from derp.history.models import Message as MessageRow
from derp.history.transcription import Transcriber
from derp.ops.owner import is_blocked


class CaptureMiddleware(BaseMiddleware):
    """Stores invocations always, ambient messages when the chat allows it, and edits in place.

    Bot commands are never stored: they are controls, not conversation. Nothing from a chat or
    person an owner blocked is stored or transcribed.

    The message an invocation replies to is part of the invocation: stored too if Derp hadn't kept
    it (privacy mode, ambient off), so its text and media are available to the run. Stored voice
    messages and video notes are transcribed before handlers run.

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
        command = starts_with_command(event)  # edits too: an edited command is still a command
        async with db.transaction() as session:
            chat = await upsert_chat(session, event.chat)
            if event.from_user is not None:
                await upsert_user(session, event.from_user)
            blocked = await is_blocked(session, chat.id, event.from_user.id if event.from_user else None)
            stored = False
            if (
                not command
                and not blocked
                and (trigger is not None or chat.type == "private" or chat.ambient)
            ):
                stored = await store_message(
                    session, event, capture="invocation" if trigger else "ambient", bot_id=me.id
                )
            replied = event.reply_to_message if trigger is not None and not command and not blocked else None
            if (
                replied is not None
                and await session.get(MessageRow, (chat.id, replied.message_id)) is not None
            ):
                replied = None  # already kept, with more detail than the embedded copy
            if replied is not None and not await store_message(
                session, replied, capture="invocation", bot_id=me.id
            ):
                replied = None
            if not self.edits:
                await track_message(session, event, stored=stored)
                await index_sticker(session, event, me.id)
        transcriber: Transcriber | None = data.get("transcriber")
        if transcriber is not None and not self.edits:
            for message in (event if stored else None, replied):
                if message is not None and (message.voice or message.video_note):
                    await transcriber.transcribe_message(event.chat.id, message.message_id)
        data["trigger"] = trigger
        data["chat_row"] = chat
        return await handler(event, data)
