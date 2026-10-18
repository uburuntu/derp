"""Quote stickers: chat messages drawn as Telegram-looking bubbles (``derp.tools.quotes``) and sent.

Only this chat's stored messages are quoted — named by id, or the one replied to and those before it in
its topic — never forgotten (tombstoned) ones or ones past the chat's retention. Avatars are Telegram
profile photos (or the chat photo for anonymous admins and channels) and media the stored files, both
fetched through the media gateway with this bot's file ids; whatever can't be fetched draws as initials
or is left out. "Quote the funniest moment" is the model finding the ids with the history tools first.
The drawing is light CPU in the local worker pool, so there is no fee.
"""

import asyncio
import zlib
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aiogram.types import User as TgUser
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import ColumnElement, select

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Refused, check_budget, emit
from derp.agent.tools.effects import PAID_AND_SPONSORED
from derp.agent.tools.sources import replied_to
from derp.agent.toolsets import Family
from derp.history.models import Chat, Message, MessageMedia, User
from derp.history.retention import kept_days
from derp.locales import catalog, resolve, t
from derp.telegram.media import TGS, MediaUnavailableError, chat_photo_bytes, profile_photo_bytes
from derp.telegram.text import utf16_len
from derp.tools import effects, quotes
from derp.tools.workers import EffectError

toolset = FunctionToolset[RunDeps]()
AVATAR_SIDE = 160  # the smallest profile photo size worth drawing
PICTURES = ("photo", "sticker", "animation", "video", "video_note", "document")


@dataclass(frozen=True, slots=True)
class Who:
    """Who a bubble shows: the colour key, the name and where their picture comes from."""

    peer: int
    name: str
    user_id: int | None = None  # profile photo
    chat_id: int | None = None  # chat photo


@toolset.tool(sequential=True)
async def make_quote(
    ctx: RunContext[RunDeps],
    message_ids: list[int] | None = None,
    count: int | None = None,
    output: Literal["sticker", "image"] = "sticker",
    background: str | None = None,
) -> str:
    """Make a Telegram-style quote of chat messages (names, avatars, replies, formatting) and send it.

    For "quote the funniest/best moment" find the messages with the history tools first, then pass their ids.

    Args:
        message_ids: The stored messages to quote by #id (up to 10), shown oldest first.
        count: Instead of ids: this many messages ending with the one replied to (or the last ones before this ask).
        output: sticker, or image (a PNG file).
        background: Bubble colour if they ask for one: a name ("pink") or hex ("#1b1429").
    """
    deps = ctx.deps
    try:
        check_budget(deps)
        rows, missing = await _chosen(deps, message_ids, count)
        data = await effects.quote(deps.workers, await _quoted(deps, rows), output, background)
        if output == "sticker":
            made = Made("sticker", data, "quote.webp", "quote sticker")
        else:
            made = Made("document", data, "quote.png", "quote")
        result = await emit(deps, made, final=True)
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"The quote failed ({error.reason})."
    gone = ", ".join(f"#{i}" for i in missing)
    return f"{result} Left out, not stored (expired or forgotten): {gone}." if missing else result


def _live(deps: RunDeps) -> list[ColumnElement[bool]]:
    """This chat's messages that may be shown: not forgotten, within retention, not service events."""
    since = datetime.now(UTC) - timedelta(days=kept_days(deps.chat, deps.settings.retention))
    return [
        Message.chat_id == deps.chat.id,
        Message.deleted_at.is_(None),
        Message.date >= since,
        Message.kind != "service",
    ]


async def _chosen(
    deps: RunDeps, message_ids: list[int] | None, count: int | None
) -> tuple[list[Message], list[int]]:
    """The messages to quote, oldest first, and the named ids that aren't stored."""
    if message_ids:
        wanted = list(dict.fromkeys(message_ids))[: quotes.MAX_MESSAGES]
        async with deps.db.session() as session:
            rows = list(
                await session.scalars(
                    select(Message)
                    .where(*_live(deps), Message.message_id.in_(wanted))
                    .order_by(Message.message_id)
                )
            )
        found = {row.message_id for row in rows}
        if not rows:
            raise Refused("None of those messages are stored here (expired or forgotten); check the ids.")
        return rows, [i for i in wanted if i not in found]
    anchor = await replied_to(deps)
    if anchor is None and count is None:
        raise Refused(
            "Which messages? Reply to one, or pass message_ids (find them with the history tools) or count."
        )
    thread = deps.target.thread_id
    topic = Message.thread_id == thread if thread is not None else Message.thread_id.is_(None)
    upto = Message.message_id <= anchor if anchor else Message.message_id < deps.trigger_message_id
    limit = max(1, min(count or 1, quotes.MAX_MESSAGES))
    async with deps.db.session() as session:
        rows = list(
            await session.scalars(
                select(Message)
                .where(*_live(deps), topic, upto)
                .order_by(Message.message_id.desc())
                .limit(limit)
            )
        )
    if not rows or (anchor is not None and rows[0].message_id != anchor):
        raise Refused("That message isn't stored (expired or forgotten), so it can't be quoted.")
    return rows[::-1], []


def _full_name(person: dict[str, Any]) -> str:
    return " ".join(str(p) for p in (person.get("first_name"), person.get("last_name")) if p) or "?"


def _who(row: Message, users: dict[int, User], titles: dict[int, str], me: TgUser) -> Who:
    """The bubble's sender: a forward's original author when Telegram names them, else who sent it."""
    origin: dict[str, Any] = row.forward_origin or {}
    match origin.get("type"):
        case "user":
            person: dict[str, Any] = origin.get("sender_user") or {}
            return Who(int(person.get("id") or 0), _full_name(person), user_id=int(person.get("id") or 0))
        case "hidden_user":
            name = str(origin.get("sender_user_name") or "?")
            return Who(zlib.crc32(name.encode()), name)
        case "chat" | "channel":
            source: dict[str, Any] = origin.get("sender_chat") or origin.get("chat") or {}
            chat_id = int(source.get("id") or 0)
            return Who(chat_id, str(source.get("title") or "?"), chat_id=chat_id or None)
        case _:
            pass
    if row.is_derp:
        return Who(me.id, me.first_name, user_id=me.id)
    if row.sender_chat_id is not None:
        return Who(row.sender_chat_id, titles.get(row.sender_chat_id, "?"), chat_id=row.sender_chat_id)
    if row.user_id is not None:
        user = users.get(row.user_id)
        return Who(row.user_id, user.full_name if user else "?", user_id=row.user_id)
    return Who(0, "?")


def _label(locale: str, kind: str) -> str:
    key = f"quote.kinds.{kind}"
    return t(locale, key if key in catalog(resolve(locale)) else "quote.kinds.other")


async def _quiet(fetch: Awaitable[bytes]) -> bytes | None:
    try:
        return await fetch
    except MediaUnavailableError:
        return None


async def _avatar(deps: RunDeps, who: Who) -> bytes | None:
    if who.user_id:
        return await _quiet(profile_photo_bytes(deps.bot, deps.gateway, who.user_id, AVATAR_SIDE))
    if who.chat_id:
        return await _quiet(chat_photo_bytes(deps.bot, deps.gateway, who.chat_id))
    return None


async def _picture(deps: RunDeps, media: MessageMedia | None) -> bytes | None:
    """A still of the message's media: the file for photos, static stickers and image files, else its preview."""
    if media is None:
        return None
    files: list[tuple[str, str | None, int | None]] = []
    own_image = media.type == "photo" or (
        media.type == "document" and (media.mime_type or "").startswith("image/")
    )
    static_sticker = media.type == "sticker" and media.mime_type not in (TGS, "video/webm")
    if own_image or static_sticker:
        files.append((media.file_id, media.file_unique_id, media.file_size))
    if media.thumb_file_id:
        files.append((media.thumb_file_id, media.thumb_unique_id, None))
    for file_id, key, size in files:
        data = await _quiet(deps.gateway.fetch(file_id, bot_id=media.bot_id, key=key, size_hint=size))
        if data is not None:
            return data
    return None


async def _quoted(deps: RunDeps, rows: list[Message]) -> list[quotes.Quoted]:
    """The rows as the renderer's messages: senders, avatars, media and reply bars resolved."""
    ids = {row.message_id for row in rows}
    reply_ids = {
        r.reply_to_message_id for r in rows if r.reply_to_message_id and r.reply_to_message_id not in ids
    }
    async with deps.db.session() as session:
        replied = {
            r.message_id: r
            for r in await session.scalars(
                select(Message).where(*_live(deps), Message.message_id.in_(reply_ids))
            )
        }
        everyone = [*rows, *replied.values()]
        user_ids = {r.user_id for r in everyone if r.user_id is not None}
        users = {u.id: u for u in await session.scalars(select(User).where(User.id.in_(user_ids)))}
        chat_ids = {r.sender_chat_id for r in everyone if r.sender_chat_id is not None}
        titles = {
            c.id: c.title or "?" for c in await session.scalars(select(Chat).where(Chat.id.in_(chat_ids)))
        }
        media: dict[int, MessageMedia] = {}
        for item in await session.scalars(
            select(MessageMedia)
            .where(
                MessageMedia.chat_id == deps.chat.id,
                MessageMedia.message_id.in_(ids),
                MessageMedia.type.in_(PICTURES),
            )
            .order_by(MessageMedia.message_id, MessageMedia.idx)
        ):
            media.setdefault(item.message_id, item)
    me = await deps.bot.me()
    senders = [_who(row, users, titles, me) for row in rows]
    unique = list(dict.fromkeys(senders))
    avatars = dict(zip(unique, await asyncio.gather(*(_avatar(deps, w) for w in unique)), strict=True))
    pictures = await asyncio.gather(*(_picture(deps, media.get(row.message_id)) for row in rows))
    locale = deps.chat.locale or deps.locale
    quoted: list[quotes.Quoted] = []
    for row, who, picture in zip(rows, senders, pictures, strict=True):
        text, entities = row.text or "", row.entities or []
        if not text and picture is None:
            text = _label(locale, row.kind)
            entities = [{"type": "italic", "offset": 0, "length": utf16_len(text)}]
        reply = None
        if (target := replied.get(row.reply_to_message_id or 0)) is not None:
            author = _who(target, users, titles, me)
            line = row.quote or target.text or _label(locale, target.kind)
            reply = quotes.Reply(author.name, author.peer, line)
        sticker = row.message_id in media and media[row.message_id].type == "sticker"
        quoted.append(
            quotes.Quoted(
                who.peer, who.name, text, entities, avatars[who], picture, sticker=sticker, reply=reply
            )
        )
    return quoted


family = Family("quotes", toolset, tiers=PAID_AND_SPONSORED, deferred=True)
