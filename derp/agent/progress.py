"""Showing that Derp is working: drafts in private chats, a status reaction in groups.

Private chats stream the answer into a rich-message draft (throttled well
under Telegram's 20 drafts / 5 s), with a thinking block naming the current
step and a stop button. Groups get one status reaction on the trigger that
changes by stage and is removed when the answer lands. When Telegram refuses
either (reactions restricted, drafts unavailable), a typing indicator stands in.
Progress never fails a run: every Telegram error here is swallowed.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputRichMessage, ReactionTypeEmoji, ReactionTypeUnion

from derp.telegram.sender import Target, sanitize_untrusted
from derp.telegram.text import RICH_LIMIT

log = logging.getLogger("derp.progress")
DRAFT_INTERVAL = 0.4  # seconds between drafts: ≤ 12.5 per 5 s
TYPING_INTERVAL = 4.5  # Telegram shows "typing…" for ~5 s
DRAFT_TAIL = 3000  # characters of a long partial answer shown in the draft
SEARCH_TOOLS = ("search", "read_messages", "chat_query", "chat_info", "inspect_media", "fetch", "digest")
DRAW_TOOLS = ("image", "meme", "sticker", "background", "reverse")

# Reaction stages; only Telegram's standard reaction emoji are allowed (no 🎨, so drawing is ✍).
THINKING, SEARCHING, DRAWING, WORKING = "👀", "🤔", "✍", "👨‍💻"


def stage_for_tool(tool_name: str) -> str:
    if any(key in tool_name for key in DRAW_TOOLS):
        return DRAWING
    if any(key in tool_name for key in SEARCH_TOOLS):
        return SEARCHING
    return WORKING


STATUS_TEXT = {
    THINKING: "Thinking…",
    SEARCHING: "Looking things up…",
    DRAWING: "Drawing…",
    WORKING: "Working…",
}


class Progress(Protocol):
    draft_id: int | None

    async def start(self) -> None: ...
    async def stage(self, stage: str) -> None: ...
    async def text(self, partial: str) -> None: ...
    def keep_reaction(self) -> None: ...
    async def close(self) -> None: ...


class Typing:
    """Fallback indicator: a chat action refreshed until closed."""

    def __init__(self, bot: Bot, target: Target) -> None:
        self.bot = bot
        self.target = target
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.bot.send_chat_action(
                    self.target.chat_id, "typing", message_thread_id=self.target.thread_id
                )
            except TelegramAPIError:
                return
            await asyncio.sleep(TYPING_INTERVAL)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class DraftProgress:
    """Private chats: the answer streams into a draft; the final message replaces it."""

    def __init__(self, bot: Bot, target: Target, *, interval: float = DRAFT_INTERVAL) -> None:
        self.bot = bot
        self.target = target
        self.interval = interval
        self.draft_id: int | None = secrets.randbelow(2**31 - 1) + 1
        self._status = STATUS_TEXT[THINKING]
        self._partial = ""
        self._dirty = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._typing = Typing(bot, target)

    async def start(self) -> None:
        self._dirty.set()
        self._task = asyncio.create_task(self._loop())

    async def stage(self, stage: str) -> None:
        self._status = STATUS_TEXT.get(stage, STATUS_TEXT[WORKING])
        self._dirty.set()

    async def text(self, partial: str) -> None:
        self._partial = partial
        self._status = ""
        self._dirty.set()

    def keep_reaction(self) -> None:
        return None

    def render(self) -> str:
        body = sanitize_untrusted(self._partial)
        if len(body) > DRAFT_TAIL:
            body = "…" + body[-DRAFT_TAIL:]
        thinking = f"<tg-thinking>{self._status}</tg-thinking>" if self._status else ""
        return "\n\n".join(p for p in (body, thinking) if p)[:RICH_LIMIT] or "<tg-thinking>…</tg-thinking>"

    async def _loop(self) -> None:
        last = 0.0
        while True:
            await self._dirty.wait()
            await asyncio.sleep(max(0.0, last + self.interval - time.monotonic()))
            self._dirty.clear()
            last = time.monotonic()
            try:
                await self.bot.send_rich_message_draft(
                    chat_id=self.target.chat_id,
                    draft_id=self.draft_id or 1,
                    rich_message=InputRichMessage(markdown=self.render()),
                    can_stop=True,
                )
            except TelegramAPIError as error:
                log.info("drafts unavailable: %s", type(error).__name__)
                self.draft_id = None
                self._typing.start()
                return

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._typing.stop()


class ReactionProgress:
    """Groups: one status reaction on the trigger, removed when the answer is out."""

    draft_id: int | None = None

    def __init__(self, bot: Bot, target: Target) -> None:
        self.bot = bot
        self.target = target
        self._current: str | None = None
        self._typing = Typing(bot, target)
        self._disabled = target.reply_to is None

    async def start(self) -> None:
        await self.stage(THINKING)

    async def stage(self, stage: str) -> None:
        if self._disabled:
            self._typing.start()
            return
        if stage == self._current:
            return
        self._current = stage
        await self._react([ReactionTypeEmoji(emoji=stage)])

    async def text(self, partial: str) -> None:
        return None

    def keep_reaction(self) -> None:
        self._current = None
        self._disabled = True  # later stages must not overwrite it either

    async def _react(self, reaction: list[ReactionTypeUnion]) -> None:
        if self.target.reply_to is None:
            return
        try:
            await self.bot.set_message_reaction(self.target.chat_id, self.target.reply_to, reaction=reaction)
        except TelegramAPIError as error:  # reactions restricted in this chat
            log.info("status reaction refused: %s", type(error).__name__)
            self._disabled = True
            self._typing.start()

    async def close(self) -> None:
        await self._typing.stop()
        if self._current is not None and not self._disabled:
            await self._react([])
