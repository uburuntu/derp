"""What chats and key owners hear when a contributed key stops serving, and a refusal's notice.

A key that hit its monthly cap, ran out of provider credits or stopped working is announced once per
pause in every chat it serves (a silent line: the usual model answers there from now on) and once to its
owner by DM, with what to do. Announcing happens when a chat is next asked (``announce``, called with
each reservation), so nothing is posted into chats nobody is using. An explicit refusal from a key's
model is shown in the chat as a reply to the ask, with Pause and Remove buttons for the key's owner and
the chat's admins, at most once per ``refusal_notice_hours`` per key and chat; the usual model then
answers. Money and privacy copy stays plain.
"""

import contextlib
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.keys.failures import KeyFailure
from derp.keys.models import ContributedKey, KeyChat
from derp.keys.routing import curated_model, next_cycle, pause
from derp.locales import resolve, t
from derp.telegram.buttons import button, markup
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal

log = logging.getLogger("derp.keys")
BRANDS = {"openai": "ChatGPT", "anthropic": "Claude"}
QUOTE_CHARS = 300


class KeyAction(CallbackData, prefix="key"):
    """Buttons on a key: its owner's key page and the chat cards (``keys.flows`` handles them)."""

    action: str  # see ``keys.flows.ACTIONS``
    key: int
    value: str = ""


def brand(provider: str) -> str:
    return BRANDS.get(provider, provider)


def owner_name(user: User | None, locale: str) -> str:
    """How a chat names a key's owner: by name, unless they chose to stay a mysterious patron."""
    if user is None or not user.patron_named or not user.full_name:
        return t(locale, "patron.mystery")
    return f"**{literal(one_line(user.full_name))}**"


def when(moment: datetime) -> str:
    """A date as a Telegram date-time entity: each reader sees it in their own calendar."""
    return f"![{moment:%Y-%m-%d}](tg://time?unix={int(moment.timestamp())}&format=D)"


def model_label(settings: Settings, model: str) -> str:
    listed = curated_model(settings, model)
    return listed.label if listed else model.split("/")[-1]


def _stopped(now: datetime) -> list[ColumnElement[bool]]:
    """Keys that stopped serving: dead, or paused by a cap or empty credits that hasn't lapsed."""
    return [
        ContributedKey.removed_at.is_(None),
        or_(
            ContributedKey.dead_at.is_not(None),
            ContributedKey.paused.in_(("cap", "exhausted")) & (ContributedKey.paused_until > now),
        ),
    ]


def _why(key: ContributedKey) -> str:
    return "dead" if key.dead_at is not None else key.paused or "cap"


async def announce(db: Database, bot: Bot, chat: Chat, thread_id: int | None, locale: str) -> None:
    """Say once in this chat, and once to each owner, that a key stopped serving here."""
    now = datetime.now(UTC)
    stopped = (
        select(KeyChat.key_id)
        .join(ContributedKey, ContributedKey.id == KeyChat.key_id)
        .where(KeyChat.chat_id == chat.id, KeyChat.state == "on", KeyChat.paused_noticed_at.is_(None))
        .where(*_stopped(now))
        .limit(1)
    )
    async with db.session() as session:  # most chats have no key that stopped: no locking transaction then
        if await session.scalar(stopped) is None:
            return
    async with db.transaction() as session:
        rows = (
            await session.execute(
                select(KeyChat, ContributedKey, User)
                .join(ContributedKey, ContributedKey.id == KeyChat.key_id)
                .outerjoin(User, User.id == ContributedKey.user_id)
                .where(KeyChat.chat_id == chat.id, KeyChat.state == "on", KeyChat.paused_noticed_at.is_(None))
                .where(*_stopped(now))
                .with_for_update(of=KeyChat)
            )
        ).all()
        owed: list[tuple[ContributedKey, User | None]] = []
        for link, key, owner in rows:
            link.paused_noticed_at = now
            if key.noticed_at is None:
                key.noticed_at = now
                owed.append((key, owner))
    public = chat.locale or locale
    for _link, key, owner in rows:
        if chat.type == "private":
            continue  # its owner's own chat: the DM says it
        text = t(
            public, f"keys.{_why(key)}_chat", who=owner_name(owner, public), brand=brand(key.provider),
            date=when(key.paused_until) if key.paused_until else "",
        )  # fmt: skip
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(bot, Target(chat.id, thread_id), text, silent=True)
    for key, owner in owed:
        await tell_owner(bot, key, owner, chat if chat.type != "private" else None)


async def tell_owner(bot: Bot, key: ContributedKey, owner: User | None, chat: Chat | None) -> None:
    """DM a key's owner why it stopped serving and what to do."""
    locale = resolve(owner.language_code if owner else None)
    text = t(
        locale, f"keys.{_why(key)}_dm", brand=brand(key.provider), label=literal(key.label),
        cap=f"${key.cap_usd:g}", date=when(key.paused_until) if key.paused_until else "",
        chat=t(locale, "keys.in_chat", title=literal(one_line(chat.title or "", 60))) if chat is not None else "",
    )  # fmt: skip
    with contextlib.suppress(TelegramAPIError):
        await send_markdown(bot, Target(key.user_id), text)


async def key_failed(
    db: Database, bot: Bot, settings: Settings, chat: Chat, target: Target, failure: KeyFailure, locale: str
) -> None:
    """Record what a failed key run says about its key; a refusal is shown in the chat (rate-limited)."""
    now = datetime.now(UTC)
    async with db.transaction() as session:
        key = await session.get(ContributedKey, failure.key_id, with_for_update=True)
        if key is None:
            return
        match failure.kind:
            case "dead" if key.dead_at is None:
                key.dead_at, key.noticed_at = now, None
                await _rearm(session, key.id)
            case "exhausted":
                await pause(session, key, "exhausted", next_cycle(key.cycle_anchor, now))
            case "refused":
                link = await session.get(KeyChat, (key.id, chat.id), with_for_update=True)
                window = timedelta(hours=settings.keys.refusal_notice_hours)
                if link is None or (
                    link.refusal_noticed_at is not None and now - link.refusal_noticed_at < window
                ):
                    return
                link.refusal_noticed_at = now
            case _:
                return
        owner = await session.get(User, key.user_id)
    if failure.kind != "refused":
        return
    public = chat.locale or locale
    said = literal(one_line(failure.said, QUOTE_CHARS)) or t(public, "keys.refused_silent")
    text = t(
        public, "keys.refused", model=model_label(settings, key.model), who=owner_name(owner, public),
        said=said,
    )  # fmt: skip
    buttons = markup(
        [
            button(t(public, "keys.pause_button"), KeyAction(action="here_pause", key=key.id)),
            button(t(public, "keys.remove_button"), KeyAction(action="here_off", key=key.id)),
        ]
    )
    with contextlib.suppress(TelegramAPIError):
        await send_markdown(bot, target, text, reply_markup=buttons)


async def _rearm(session: AsyncSession, key_id: int) -> None:
    """Every chat the key serves hears about its new state once."""
    for link in await session.scalars(select(KeyChat).where(KeyChat.key_id == key_id)):
        link.paused_noticed_at = None
