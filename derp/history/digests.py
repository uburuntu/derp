"""Day digests: which messages a chat day holds, the cached summaries, and staleness.

Days are calendar days in the chat's time zone (``Scope.tz``). A digest is
keyed by chat, scope (0 = the whole chat, otherwise a forum topic) and day;
building one is the agent's job (it needs a model). An edit of a message inside
a digest's id range, or Derp deleting its own message there, marks it stale
(never shown, rebuilt on the next ask), whatever its scope. Forgetting deletes
covering digests instead (``retention.py``): nothing summarizing what someone
took back may stay stored.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.models import Digest, Message
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
