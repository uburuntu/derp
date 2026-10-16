"""Render stored messages as compact transcript lines for models and history tools.

One line per message: ``[10:42] #123 Ann ↩#120: text [photo: a cat] {🔥3}``.
Media show their stored description or transcript when one exists; payloads
(dice, polls, locations, service events…) become short bracketed notes.
Contacts render without phone numbers: the window never needs them.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from derp.history.models import Chat, Message, MessageMedia, Reaction, ReactionCount, User

_WRAPPER_TAG = re.compile(
    r"<(?=\s*/?\s*(?:chat_history|current_message|notes|chat_profile|situation)\b)", re.IGNORECASE
)
CONTINUATION = "\n  ┆ "


def neutralize(text: str) -> str:
    """Untrusted text can't close Derp's data blocks or start a line that looks like a new message."""
    return _WRAPPER_TAG.sub("‹", text).replace("\n", CONTINUATION)


def one_line(text: str, limit: int = 64) -> str:
    """A user-controlled name or title for the prompt: one line, short, no wrapper tags."""
    return neutralize(" ".join(text.split()))[:limit]


DERP_NAME = "Derp"
TEXT_LIMIT = 2000  # characters of one message kept in the window; longer texts are cut with "…"


@dataclass(slots=True)
class MessageView:
    row: Message
    sender: str
    media: list[MessageMedia] = field(default_factory=list[MessageMedia])
    reactions: dict[str, int] = field(default_factory=dict[str, int])


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: UTF-8 bytes / 4 (Cyrillic counts double, as tokenizers do)."""
    return len(text.encode()) // 4 + 1


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, secs = divmod(seconds, 60)
    return f" {minutes}:{secs:02d}"


def render_media(media: MessageMedia) -> str:
    detail = media.transcript or media.description
    match media.type:
        case "sticker":
            label = f"sticker {media.sticker_emoji or ''}".rstrip()
        case "voice" | "video_note" | "video" | "audio":
            label = media.type.replace("_", " ") + _duration(media.duration)
        case "document":
            label = f"file {media.file_name}" if media.file_name else "file"
        case "animation":
            label = "gif"
        case _:
            label = media.type
    return f"[{label}: {_clip(detail, 600)}]" if detail else f"[{label}]"


def _service(payload: dict[str, Any]) -> str:
    event = str(payload.get("event", "event"))
    data = payload.get("data")
    match event:
        case "new_chat_members" if isinstance(data, list):
            names = ", ".join(str(p.get("name", "")) for p in data)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
            return f"[joined: {names}]"
        case "left_chat_member" if isinstance(data, dict):
            return f"[left: {data.get('name', '')}]"  # pyright: ignore[reportUnknownMemberType]
        case "new_chat_title":
            return f"[renamed the chat: {data}]"
        case "pinned_message" if isinstance(data, dict):
            return f"[pinned #{data.get('message_id')}]"  # pyright: ignore[reportUnknownMemberType]
        case _:
            return f"[{event.replace('_', ' ')}]"


def render_payload(kind: str, payload: dict[str, Any]) -> str:
    match kind:
        case "dice":
            return f"[dice {payload.get('emoji', '🎲')} = {payload.get('value')}]"
        case "poll":
            options = " / ".join(str(o) for o in payload.get("options", []))
            return f"[poll: {payload.get('question', '')} — {options}]"
        case "location":
            return f"[location {payload.get('latitude'):.4f},{payload.get('longitude'):.4f}]"
        case "venue":
            return f"[venue: {payload.get('title', '')}, {payload.get('address', '')}]"
        case "contact":
            name = " ".join(filter(None, (payload.get("first_name"), payload.get("last_name"))))
            return f"[contact: {name}]"
        case "checklist":
            return f"[checklist: {payload.get('title', '')}]"
        case "service":
            return _service(payload)
        case (
            "text"
            | "photo"
            | "video"
            | "voice"
            | "video_note"
            | "audio"
            | "document"
            | "sticker"
            | "animation"
        ):
            return ""
        case _:
            return f"[{kind.replace('_', ' ')}]"


def _field(data: dict[str, Any], outer: str, inner: str) -> str | None:
    nested: Any = data.get(outer)
    if not isinstance(nested, dict):
        return None
    value = cast(dict[str, Any], nested).get(inner)
    return str(value) if value else None


def _forward(origin: dict[str, Any] | None) -> str:
    if not origin:
        return ""
    source = (
        _field(origin, "sender_user", "first_name")
        or origin.get("sender_user_name")
        or _field(origin, "chat", "title")
        or _field(origin, "sender_chat", "title")
        or "someone"
    )
    return f" (fwd from {source})"


def render_body(view: MessageView) -> str:
    """Text, media notes and payload note of one message, without the header."""
    row = view.row
    parts: list[str] = []
    if row.text:
        parts.append(_clip(row.text))
    parts.extend(render_media(m) for m in view.media)
    if note := render_payload(row.kind, row.payload):
        parts.append(note)
    return " ".join(parts) or "[empty]"


def render_line(view: MessageView, tz: tzinfo) -> str:
    row = view.row
    reply = f" ↩#{row.reply_to_message_id}" if row.reply_to_message_id else ""
    quote = f" «{_clip(row.quote, 200)}»" if row.quote else ""
    edited = " (edited)" if row.edit_date else ""
    reactions = (
        " {" + " ".join(f"{k}{n}" for k, n in sorted(view.reactions.items(), key=lambda kv: -kv[1])) + "}"
        if view.reactions
        else ""
    )
    when = row.date.astimezone(tz).strftime("%H:%M")
    header = f"[{when}] #{row.message_id} {one_line(view.sender)}{_forward(row.forward_origin)}{reply}{quote}{edited}"
    return neutralize(f"{header}: {render_body(view)}{reactions}")


def render_transcript(views: Iterable[MessageView], tz: tzinfo) -> str:
    """Lines with a day header whenever the date changes."""
    lines: list[str] = []
    day = None
    for view in views:
        current = view.row.date.astimezone(tz).date()
        if current != day:
            day = current
            lines.append(f"— {current.strftime('%a %Y-%m-%d')} —")
        lines.append(render_line(view, tz))
    return "\n".join(lines)


def _custom_or(reaction: str) -> str:
    return "custom-emoji" if reaction.startswith("custom:") else reaction


async def load_views(session: AsyncSession, rows: Sequence[Message]) -> list[MessageView]:
    """Attach sender names, media and reaction totals to message rows (one query each)."""
    if not rows:
        return []
    chat_id = rows[0].chat_id
    ids = [r.message_id for r in rows]
    user_ids = {r.user_id for r in rows if r.user_id is not None}
    sender_chats = {r.sender_chat_id for r in rows if r.sender_chat_id is not None}
    names: dict[int, str] = {}
    if user_ids:
        for user in await session.scalars(select(User).where(User.id.in_(user_ids))):
            names[user.id] = user.full_name
    chat_titles: dict[int, str] = {}
    if sender_chats:
        sender = aliased(Chat)
        for sender_id, title in await session.execute(
            select(sender.id, sender.title).where(sender.id.in_(sender_chats))
        ):
            chat_titles[sender_id] = title or "a channel"
    media: dict[int, list[MessageMedia]] = defaultdict(list)
    for item in await session.scalars(
        select(MessageMedia)
        .where(MessageMedia.chat_id == chat_id, MessageMedia.message_id.in_(ids))
        .order_by(MessageMedia.message_id, MessageMedia.idx)
    ):
        media[item.message_id].append(item)
    # Anonymous totals already include every actor, so they replace per-actor rows when present.
    reactions: dict[int, dict[str, int]] = defaultdict(dict)
    for message_id, reaction, count in await session.execute(
        select(ReactionCount.message_id, ReactionCount.reaction, ReactionCount.count).where(
            ReactionCount.chat_id == chat_id, ReactionCount.message_id.in_(ids)
        )
    ):
        key = _custom_or(reaction)
        reactions[message_id][key] = reactions[message_id].get(key, 0) + count
    totals = set(reactions)
    for message_id, reaction, count in await session.execute(
        select(Reaction.message_id, Reaction.reaction, func.count())
        .where(Reaction.chat_id == chat_id, Reaction.message_id.in_(ids))
        .group_by(Reaction.message_id, Reaction.reaction)
    ):
        if message_id not in totals:
            key = _custom_or(reaction)
            reactions[message_id][key] = reactions[message_id].get(key, 0) + count
    views: list[MessageView] = []
    for row in rows:
        if row.is_derp:
            sender = DERP_NAME
        elif row.user_id is not None and row.sender_chat_id is None:
            sender = names.get(row.user_id, f"user {row.user_id}")
        elif row.sender_chat_id == row.chat_id:
            sender = "Anonymous admin"
        elif row.sender_chat_id is not None:
            sender = chat_titles.get(row.sender_chat_id, "a channel")
        else:
            sender = "someone"
        views.append(
            MessageView(row, sender, media.get(row.message_id, []), reactions.get(row.message_id, {}))
        )
    return views


def chat_time(chat: Chat | None) -> tzinfo:
    if chat is not None and chat.timezone:
        try:
            return ZoneInfo(chat.timezone)
        except ZoneInfoNotFoundError, ValueError:
            pass
    return ZoneInfo("UTC")


def describe_time(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z")
