"""Day digests: which messages a chat day holds, the cached summaries, and staleness.

Days are calendar days in the chat's time zone (``Scope.tz``). A digest is
keyed by chat, scope (0 = the whole chat, otherwise a forum topic) and day;
building one is the agent's job (it needs a model). Catching someone up from a
message reads the rest of that message's day fresh (``span_messages``, never
cached) and the whole days after it from digests; ``aimed_at`` finds what in a
stretch replies to or mentions the person asking. An edit of a message inside
a digest's id range, or Derp deleting its own message there, marks it stale
(never shown, rebuilt on the next ask), whatever its scope. Forgetting deletes
covering digests instead (``retention.py``): nothing summarizing what someone
took back may stay stored.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from derp.history.models import ChatMember, Digest, Message, User
from derp.history.queries import Scope


def scope_key(scope: Scope) -> int:
    return 0 if scope.whole_chat or scope.thread_id is None else scope.thread_id


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), tz)
    return start.astimezone(UTC), datetime.combine(day + timedelta(days=1), time(), tz).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DayStats:
    first_id: int
    last_id: int
    count: int


async def day_stats(session: AsyncSession, scope: Scope, day: date) -> DayStats | None:
    start, end = day_bounds(day, scope.tz)
    first, last, count = (
        await session.execute(
            select(func.min(Message.message_id), func.max(Message.message_id), func.count()).where(
                scope.messages(), Message.date >= start, Message.date < end
            )
        )
    ).one()
    return DayStats(first, last, count) if count else None


async def day_messages(session: AsyncSession, scope: Scope, day: date) -> list[Message]:
    start, end = day_bounds(day, scope.tz)
    rows = await session.scalars(
        select(Message)
        .where(scope.messages(), Message.date >= start, Message.date < end)
        .order_by(Message.message_id)
    )
    return list(rows)


def is_current(digest: Digest | None, stats: DayStats) -> bool:
    """A digest still describes the day: not stale and nothing newer has arrived since."""
    return digest is not None and not digest.stale and digest.last_message_id >= stats.last_id


async def save(
    session: AsyncSession, chat_id: int, key: int, day: date, stats: DayStats, summary: str, model: str
) -> Digest:
    digest = await session.get(Digest, (chat_id, key, day))
    if digest is None:
        digest = Digest(chat_id=chat_id, thread_id=key, day=day)
        session.add(digest)
    digest.first_message_id, digest.last_message_id = stats.first_id, stats.last_id
    digest.message_count, digest.summary, digest.model, digest.stale = stats.count, summary, model, False
    await session.flush()
    return digest


async def mark_stale(session: AsyncSession, chat_id: int, message_id: int) -> None:
    await session.execute(
        update(Digest)
        .where(
            Digest.chat_id == chat_id,
            Digest.first_message_id <= message_id,
            Digest.last_message_id >= message_id,
            Digest.stale.is_(False),
        )
        .values(stale=True)
    )


async def latest_before(session: AsyncSession, chat_id: int, key: int, message_id: int) -> Digest | None:
    """The newest usable digest that ends before ``message_id`` (what precedes the context window)."""
    return await session.scalar(
        select(Digest)
        .where(
            Digest.chat_id == chat_id,
            Digest.thread_id == key,
            Digest.last_message_id < message_id,
            Digest.stale.is_(False),
        )
        .order_by(Digest.day.desc())
        .limit(1)
    )


async def last_said(session: AsyncSession, scope: Scope, user_id: int, before_id: int) -> Message | None:
    """Where "since my last message" starts: the person's last message before anyone else spoke again.

    Their own messages right before asking (a burst ending in the question) don't count as having caught up.
    """
    other = await session.scalar(
        select(func.max(Message.message_id)).where(
            scope.messages(),
            Message.message_id < before_id,
            or_(Message.user_id != user_id, Message.user_id.is_(None)),
        )
    )
    if other is None:
        return None
    return await session.scalar(
        select(Message)
        .where(scope.messages(), Message.user_id == user_id, Message.message_id < other)
        .order_by(Message.message_id.desc())
        .limit(1)
    )


async def span_messages(
    session: AsyncSession, scope: Scope, after_id: int, before_id: int, until: datetime
) -> list[Message]:
    """Messages after one message and before another, ending at ``until`` (the end of the first one's day)."""
    rows = await session.scalars(
        select(Message)
        .where(
            scope.messages(),
            Message.message_id > after_id,
            Message.message_id < before_id,
            Message.date < until,
        )
        .order_by(Message.message_id)
    )
    return list(rows)


async def aimed_at(
    session: AsyncSession,
    scope: Scope,
    user_id: int,
    *,
    start: datetime,
    end: datetime | None,
    after_id: int,
    before_id: int,
    limit: int,
) -> list[Message]:
    """Others' messages in a stretch that reply to this person or mention them (@username, name, tag), oldest first."""
    person = await session.get(User, user_id)
    member = await session.get(ChatMember, (scope.chat_id, user_id))
    stems = _name_stems([person.first_name if person else None, *(member.names if member else [])])
    handles = [f"@{person.username.lower()}"] if person and person.username else []
    parent = aliased(Message)
    replied = (
        select(parent.message_id)
        .where(
            parent.chat_id == Message.chat_id,
            parent.message_id == Message.reply_to_message_id,
            parent.user_id == user_id,
        )
        .exists()
    )
    statement = select(Message, replied).where(
        scope.messages(),
        Message.date >= start,
        Message.message_id > after_id,
        Message.message_id < before_id,
        Message.is_derp.is_(False),
        or_(Message.user_id != user_id, Message.user_id.is_(None)),
        or_(
            replied,
            Message.entities.contains([{"user": {"id": user_id}}]),  # a tag of someone without a username
            *(Message.text.ilike(f"%{word}%") for word in [*stems, *handles]),
        ),
    )
    if end is not None:
        statement = statement.where(Message.date < end)
    words = [rf"(?<!\w){re.escape(stem)}\w{{0,2}}(?!\w)" for stem in stems]
    words += [rf"{re.escape(handle)}(?!\w)" for handle in handles]
    named = re.compile("|".join(words), re.IGNORECASE) if words else None
    rows = await session.execute(statement.order_by(Message.message_id.desc()).limit(limit * 4))
    found = [
        row
        for row, is_reply in rows
        if is_reply or _tags(row, user_id) or (named is not None and named.search(row.text or ""))
    ]
    return found[:limit][::-1]


def _name_stems(names: list[str | None]) -> list[str]:
    """First names as they're inflected: "Маша" → "маш" (Маше, Машу), "Ann" stays (Annie)."""
    stems: set[str] = set()
    for name in names:
        word = name.split()[0].lower() if name and name.split() else ""
        stem = word[:-1] if len(word) >= 4 and word[-1] in "aeiouyаеёиоуыэюя" else word
        if len(stem) >= 3:
            stems.add(stem)
    return sorted(stems)


def _tags(row: Message, user_id: int) -> bool:
    entities: list[dict[str, Any]] = row.entities or []
    return any(entity.get("user", {}).get("id") == user_id for entity in entities)
