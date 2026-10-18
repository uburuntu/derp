"""Capture every message before handlers run, and tell handlers whether Derp was invoked."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from derp.agent.decisions import Decider
from derp.agent.invocation import TriggerKind, detect_trigger, starts_with_command
from derp.db.engine import Database
from derp.history.capture import index_sticker, is_stored, store_message, upsert_chat, upsert_user
from derp.history.members import track_message
from derp.history.models import Message as MessageRow
from derp.history.render import chat_time
from derp.history.transcription import Transcriber
from derp.ops.owner import is_blocked


class CaptureMiddleware(BaseMiddleware):
    """Stores invocations always, ambient messages when the chat allows it, and edits in place.

    An edit to a message already kept (an invocation, say) updates it even while the chat's context
    is off; edits to messages Derp didn't keep are stored only when ambient messages would be.

    Bot commands are never stored: they are controls, not conversation. Nothing from a chat or
    person an owner blocked is stored or transcribed.

    The message an invocation replies to is part of the invocation: stored too if Derp hadn't kept
    it (privacy mode, ambient off), so its text and media are available to the run. Stored voice
    messages and video notes are transcribed before handlers run (ambient ones only under the daily
    creator cap), audio files and videos only when they are part of an invocation.

    A name-only mention in a group is an invocation only if the decision pin says it speaks to
    Derp (``agent.decisions``), asked before anything is stored: a passing mention is an ordinary
    message (stored only while the chat keeps ambient context, its reply not saved, no invocation
    transcription). Nobody blocked is screened.

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
        decider: Decider | None = data.get("decider")
        if trigger is not None and trigger.kind is TriggerKind.NAME and not blocked and decider is not None:
            verdict = await decider.decide(event, bot_id=me.id, tz=chat_time(chat), named=True)
            if not verdict.addressed:
                trigger = None
        async with db.transaction() as session:
            stored = False
            keep = trigger is not None or chat.type == "private" or chat.ambient
            if self.edits and not keep and not command and not blocked:  # an edit updates a row already kept
                keep = await is_stored(session, chat.id, event.message_id)
            if keep and not command and not blocked:
                stored = await store_message(
                    session, event, capture="invocation" if trigger else "ambient", bot_id=me.id
                )
            replied = event.reply_to_message if trigger is not None and not command and not blocked else None
            if (  # a row already kept has more detail than the embedded copy: store only if missing
                replied is not None
                and await session.get(MessageRow, (chat.id, replied.message_id)) is None
                and not await store_message(session, replied, capture="invocation", bot_id=me.id)
            ):
                replied = None
            if not self.edits:
                await track_message(session, event, stored=stored)
                await index_sticker(session, event, me.id)
        transcriber: Transcriber | None = data.get("transcriber")
        if transcriber is not None and not self.edits:
            # A replied-to recording is transcribed on demand (no-op if it already has a transcript).
            ambient = trigger is None and chat.type != "private"
            thread = event.message_thread_id if event.is_topic_message else None
            for message, capped in ((event if stored else None, ambient), (replied, False)):
                if message is not None:
                    await transcriber.transcribe(message, thread_id=thread, ambient=capped)
        data["trigger"] = trigger
        data["chat_row"] = chat
        return await handler(event, data)
