"""Keeping contributed keys tidy: unplugging a key from a chat its owner left, and removing dead keys.

A member who leaves (or is banned from) a chat stops paying for it: their keys there are unplugged at once
(``detach_member``, from the membership tracker) and the sweep tells them. A key that stopped working is
removed at OpenRouter after ``dead_days`` (its owner told by DM), so nothing of theirs lingers there.
"""

import contextlib
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.inference.api import OpenRouterError, OpenRouterManagement
from derp.keys.models import ContributedKey, KeyChat
from derp.keys.notices import brand
from derp.keys.register import take_out
from derp.locales import resolve, t
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal

log = logging.getLogger("derp.keys")


async def detach_member(session: AsyncSession, chat_id: int, user_id: int) -> None:
    """Their keys stop serving a chat they left; the sweep tells them (``off_noticed_at`` stays empty)."""
    keys = select(ContributedKey.id).where(ContributedKey.user_id == user_id)
    await session.execute(
        update(KeyChat)
        .where(KeyChat.chat_id == chat_id, KeyChat.key_id.in_(keys), KeyChat.state != "off")
        .values(state="off", off_noticed_at=None)
    )


async def sweep(db: Database, bot: Bot, settings: Settings, management: OpenRouterManagement | None) -> None:
    now = datetime.now(UTC)
    async with db.transaction() as session:
        left = (
            await session.execute(
                select(KeyChat, ContributedKey, Chat, User)
                .join(ContributedKey, ContributedKey.id == KeyChat.key_id)
                .join(Chat, Chat.id == KeyChat.chat_id)
                .outerjoin(User, User.id == ContributedKey.user_id)
                .where(KeyChat.state == "off", KeyChat.off_noticed_at.is_(None))
                .with_for_update(of=KeyChat)
            )
        ).all()
        for link, *_ in left:
            link.off_noticed_at = now
    for _link, key, chat, owner in left:
        locale = resolve(owner.language_code if owner else None)
        text = t(
            locale, "keys.left_dm", brand=brand(key.provider), chat=literal(one_line(chat.title or "", 60))
        )
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(bot, Target(key.user_id), text)
    if management is None:
        return
    async with db.session() as session:
        dead = list(
            await session.scalars(
                select(ContributedKey).where(
                    ContributedKey.removed_at.is_(None),
                    ContributedKey.dead_at < now - timedelta(days=settings.keys.dead_days),
                )
            )
        )
    for key in dead:
        try:
            await take_out(db, management, key)
        except OpenRouterError as error:
            log.info("removing a dead key failed: %s", error.status)
            continue
        async with db.session() as session:
            owner = await session.get(User, key.user_id)
        locale = resolve(owner.language_code if owner else None)
        text = t(
            locale,
            "keys.removed_dm",
            brand=brand(key.provider),
            label=literal(key.label),
            days=settings.keys.dead_days,
        )
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(bot, Target(key.user_id), text)
