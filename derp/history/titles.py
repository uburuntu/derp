"""Titles of the day ("красавчик дня"): one random winner per chat, title and chat-local day.

The winner is drawn among the chat's recently active members (still in the
chat, seen within ``ACTIVE_DAYS``) and kept, so asking again that day gives the
same person. Counts per title and person are the chat's leaderboard;
``forget_member`` drops a person's titles.
"""

import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.models import ChatMember, DailyTitle, User

ACTIVE_DAYS = 14
MAX_TITLE = 64


def normalize(title: str) -> str:
    return " ".join(title.split()).casefold()[:MAX_TITLE]


async def crown(
    session: AsyncSession, chat_id: int, title: str, day: date, now: datetime
) -> tuple[int, bool] | None:
    """Today's winner and whether they were just picked; None when nobody is eligible."""
    key = (DailyTitle.chat_id == chat_id) & (DailyTitle.title == title) & (DailyTitle.day == day)
    if (winner := await session.scalar(select(DailyTitle.user_id).where(key))) is not None:
        return winner, False
    candidates = list(
        await session.scalars(
            select(ChatMember.user_id)
            .join(User, User.id == ChatMember.user_id)
            .where(
                ChatMember.chat_id == chat_id,
                ChatMember.status.notin_(("left", "kicked")),
                ChatMember.last_seen >= now - timedelta(days=ACTIVE_DAYS),
                User.is_bot.is_(False),
            )
            .order_by(ChatMember.user_id)
        )
    )
    if not candidates:
        return None
    picked = secrets.choice(candidates)
    await session.execute(
        insert(DailyTitle)
        .values(chat_id=chat_id, title=title, day=day, user_id=picked)
        .on_conflict_do_nothing()
    )
    winner = await session.scalar(select(DailyTitle.user_id).where(key))  # a concurrent pick may have won
    return (winner, winner == picked) if winner is not None else None


async def leaderboard(
    session: AsyncSession,
    chat_id: int,
    *,
    title: str | None = None,
    contains: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 20,
) -> list[tuple[str, str, int]]:
    """(title, winner's name, wins) for one title or titles containing a text, most wins first."""
    name = func.coalesce(func.concat_ws(" ", User.first_name, User.last_name), literal("someone"))
    wins = func.count()
    statement = (
        select(DailyTitle.title, name, wins)
        .outerjoin(User, User.id == DailyTitle.user_id)
        .where(DailyTitle.chat_id == chat_id)
        .group_by(DailyTitle.title, DailyTitle.user_id, User.first_name, User.last_name)
        .order_by(wins.desc(), DailyTitle.title)
        .limit(limit)
    )
    if title is not None:
        statement = statement.where(DailyTitle.title == normalize(title))
    if contains:
        statement = statement.where(DailyTitle.title.contains(normalize(contains), autoescape=True))
    if since is not None:
        statement = statement.where(DailyTitle.day >= since)
    if until is not None:
        statement = statement.where(DailyTitle.day <= until)
    return [(row[0], row[1], int(row[2])) for row in await session.execute(statement)]
