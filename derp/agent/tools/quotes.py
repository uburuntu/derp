"""Quote stickers: chat messages drawn as Telegram-looking bubbles (``derp.tools.quotes``) and sent.

Only this chat's stored messages are quoted — named by id, or the one replied to and those before it in
its topic — never forgotten (tombstoned) ones or ones past the chat's retention. Avatars are Telegram
profile photos (or the chat photo for anonymous admins and channels) and media the stored files, both
fetched through the media gateway with this bot's file ids; whatever can't be fetched draws as initials
or is left out. "Quote the funniest moment" is the model finding the ids with the history tools first.
The drawing is light CPU in the local worker pool, so there is no fee. ``make_quote`` is one of the
effects tools (registered in ``derp.agent.tools``).
"""

import asyncio
import zlib
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aiogram.types import User as TgUser
from pydantic_ai import RunContext
from sqlalchemy import ColumnElement, select

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Refused, check_budget, emit
from derp.agent.tools.sources import replied_to
from derp.history.models import Message, MessageMedia
from derp.history.render import Author, MessageView, load_views
from derp.history.retention import kept_days
from derp.locales import catalog, resolve, t
from derp.telegram.media import TGS, MediaUnavailableError, chat_photo_bytes, profile_photo_bytes
from derp.telegram.text import utf16_len, visible
from derp.tools import effects, quotes
from derp.tools.workers import EffectError

AVATAR_SIDE = 160  # the smallest profile photo size worth drawing
PICTURES = ("photo", "sticker", "animation", "video", "video_note", "document")


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


def _who(view: MessageView, me: TgUser) -> Author:
    """The bubble's sender: who wrote it (``render.author``), Derp as the bot itself."""
    who = view.author or Author("?")
    return replace(who, name=me.first_name, user_id=me.id) if view.row.is_derp else who


def _peer(who: Author) -> int:
    """The colour key: the person or chat, else the name (a hidden account)."""
    return who.user_id or who.chat_id or zlib.crc32(who.name.encode())


def _shown(row: Message) -> tuple[str, list[dict[str, Any]]]:
    """The text as the chat saw it: Derp's own messages are stored as the Markdown it wrote."""
    if row.is_derp and row.text and not row.entities:
        return visible(row.text)
    return row.text or "", row.entities or []


def _label(locale: str, kind: str) -> str:
    key = f"quote.kinds.{kind}"
    return t(locale, key if key in catalog(resolve(locale)) else "quote.kinds.other")


async def _quiet(fetch: Awaitable[bytes]) -> bytes | None:
    try:
        return await fetch
    except MediaUnavailableError:
        return None


async def _avatar(deps: RunDeps, who: Author) -> bytes | None:
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
        replied = list(
            await session.scalars(select(Message).where(*_live(deps), Message.message_id.in_(reply_ids)))
        )
        views = {view.row.message_id: view for view in await load_views(session, [*rows, *replied])}
    me = await deps.bot.me()
    senders = [_who(views[row.message_id], me) for row in rows]
    media = {
        row.message_id: next((m for m in views[row.message_id].media if m.type in PICTURES), None)
        for row in rows
    }
    unique = list(dict.fromkeys(senders))
    avatars = dict(zip(unique, await asyncio.gather(*(_avatar(deps, w) for w in unique)), strict=True))
    pictures = await asyncio.gather(*(_picture(deps, media[row.message_id]) for row in rows))
    locale = deps.chat.locale or deps.locale
    quoted: list[quotes.Quoted] = []
    for row, who, picture in zip(rows, senders, pictures, strict=True):
        text, entities = _shown(row)
        if not text and picture is None:
            text = _label(locale, row.kind)
            entities = [{"type": "italic", "offset": 0, "length": utf16_len(text)}]
        reply = None
        if row.reply_to_message_id and (target := views.get(row.reply_to_message_id)) is not None:
            author = _who(target, me)
            line = row.quote or _shown(target.row)[0] or _label(locale, target.row.kind)
            reply = quotes.Reply(author.name, _peer(author), line)
        found = media[row.message_id]
        sticker = found is not None and found.type == "sticker"
        quoted.append(
            quotes.Quoted(
                _peer(who), who.name, text, entities, avatars[who], picture, sticker=sticker, reply=reply
            )
        )
    return quoted
