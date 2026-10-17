"""Owner pings: short plain-text DMs to the configured owners, best effort; error digests to the ops chat.

Errors are counted by logger name only (content-free) and sent as one digest per
interval, so an outage produces a line, not a flood.
"""

import logging
from collections import Counter

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.history.models import Chat, User

log = logging.getLogger("derp.owner")


async def notify_owners(
    bot: Bot, settings: Settings, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    for owner_id in settings.owner_ids:
        try:
            await bot.send_message(owner_id, text[:4000], reply_markup=reply_markup)
        except TelegramAPIError as error:  # the owner may not have started this bot
            log.info("owner ping not delivered: %s", type(error).__name__)


class ErrorTally(logging.Handler):
    """Counts ERROR and worse records per logger; never keeps their messages."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.counts: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        self.counts[record.name] += 1

    def take(self) -> Counter[str]:
        counts, self.counts = self.counts, Counter()
        return counts


async def send_error_digest(bot: Bot, settings: Settings, tally: ErrorTally, minutes: int) -> None:
    counts = tally.take()
    if not counts:
        return
    parts = ", ".join(f"{name} ×{count}" for name, count in counts.most_common(8))
    text = f"⚠️ {counts.total()} error(s) in the last {minutes} min: {parts}"
    if settings.logfire_url:
        text += f"\n{settings.logfire_url}"
    if settings.ops_chat_id is None:
        await notify_owners(bot, settings, text)
        return
    try:
        await bot.send_message(settings.ops_chat_id, text)
    except TelegramAPIError as error:  # logged below ERROR so a failing digest can't feed itself
        log.info("error digest not delivered: %s", type(error).__name__)


async def is_blocked(session: AsyncSession, chat_id: int, user_id: int | None) -> bool:
    """An owner blocked this chat or this person: Derp answers nothing."""
    if await session.scalar(select(Chat.blocked_at).where(Chat.id == chat_id)) is not None:
        return True
    return user_id is not None and (
        await session.scalar(select(User.blocked_at).where(User.id == user_id)) is not None
    )
