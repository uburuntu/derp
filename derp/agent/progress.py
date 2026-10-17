"""Showing that Derp is working: drafts in private chats, a chat action in groups.

Private chats stream the answer into a rich-message draft (throttled well
under Telegram's 20 drafts / 5 s), with a thinking block naming the current
step and a stop button. Groups get a chat action matching the stage ("typing",
"sending a photo" while drawing): unlike a reaction on the trigger, it notifies
nobody. When drafts are unavailable, the chat action stands in for them too.
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

from derp.locales import t
from derp.telegram.sender import Target, rich, sanitize_untrusted
from derp.telegram.text import RICH_LIMIT

log = logging.getLogger("derp.progress")
DRAFT_INTERVAL = 0.4  # seconds between drafts: ≤ 12.5 per 5 s
ACTION_INTERVAL = 4.5  # Telegram shows a chat action for ~5 s
DRAFT_TAIL = 3000  # characters of a long partial answer shown in the draft
SEARCH_TOOLS = ("search", "read_messages", "chat_query", "chat_info", "inspect_media", "fetch", "digest")
DRAW_TOOLS = ("image", "meme", "sticker", "background", "reverse")

THINKING, SEARCHING, DRAWING, WORKING = "thinking", "searching", "drawing", "working"
STAGES = frozenset({THINKING, SEARCHING, DRAWING, WORKING})


def stage_for_tool(tool_name: str) -> str:
    if any(key in tool_name for key in DRAW_TOOLS):
        return DRAWING
    if any(key in tool_name for key in SEARCH_TOOLS):
        return SEARCHING
    return WORKING


def action_for(stage: str) -> str:
    return "upload_photo" if stage == DRAWING else "typing"


class Progress(Protocol):
    draft_id: int | None

    async def start(self) -> None: ...
    async def stage(self, stage: str) -> None: ...
    async def text(self, partial: str) -> None: ...
    async def close(self) -> None: ...


class ChatAction:
    """A chat action refreshed until closed; a new action is shown at once."""

    def __init__(self, bot: Bot, target: Target) -> None:
        self.bot = bot
        self.target = target
        self.action = "typing"
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def show(self, action: str) -> None:
        if action != self.action:
            self.action = action
            self._changed.set()

    async def _loop(self) -> None:
        while True:
            self._changed.clear()
            try:
                await self.bot.send_chat_action(
                    self.target.chat_id, self.action, message_thread_id=self.target.thread_id
                )
            except TelegramAPIError:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), ACTION_INTERVAL)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class DraftProgress:
    """Private chats: the answer streams into a draft; the final message replaces it."""

    def __init__(
        self, bot: Bot, target: Target, *, locale: str = "en", interval: float = DRAFT_INTERVAL
    ) -> None:
        self.bot = bot
        self.target = target
        self.locale = locale
        self.interval = interval
        self.draft_id: int | None = secrets.randbelow(2**31 - 1) + 1
        self._status = t(locale, f"progress.{THINKING}")
        self._partial = ""
        self._dirty = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._fallback = ChatAction(bot, target)

    async def start(self) -> None:
        self._dirty.set()
        self._task = asyncio.create_task(self._loop())

    async def stage(self, stage: str) -> None:
        key = stage if stage in STAGES else WORKING
        self._status = t(self.locale, f"progress.{key}")
        self._fallback.show(action_for(stage))
        self._dirty.set()

    async def text(self, partial: str) -> None:
        self._partial = partial
        self._status = ""
        self._dirty.set()

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
                    rich_message=rich(self.render()),
                    can_stop=True,
                )
            except TelegramAPIError as error:
                log.info("drafts unavailable: %s", type(error).__name__)
                self.draft_id = None
                self._fallback.start()
                return

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._fallback.stop()


class ActionProgress:
    """Groups: a chat action that follows the run's stage until the answer is out."""

    draft_id: int | None = None

    def __init__(self, bot: Bot, target: Target) -> None:
        self._action = ChatAction(bot, target)

    async def start(self) -> None:
        self._action.start()

    async def stage(self, stage: str) -> None:
        self._action.show(action_for(stage))

    async def text(self, partial: str) -> None:
        return None

    async def close(self) -> None:
        await self._action.stop()
