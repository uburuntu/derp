from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from derp.config import Retention
from derp.db.engine import Database
from derp.history.models import (
    Chat,
    ChatMember,
    Digest,
    MemberEvent,
    Message,
    MessageMedia,
    Reaction,
)
from derp.history.retention import forget_member, purge_ambient, purge_expired

NOW = datetime.now(UTC)
GROUP, PRIVATE, SHORT = -720, 72, -721


def msg(
    chat_id: int, message_id: int, *, days_ago: float, user_id: int = 1, capture: str = "ambient"
) -> Message:
    return Message(
        chat_id=chat_id, message_id=message_id, user_id=user_id, date=NOW - timedelta(days=days_ago),
        kind="text", text=f"m{message_id}", capture=capture, payload={},
    )  # fmt: skip


def digest(chat_id: int, day: date, first: int, last: int, *, stale: bool = False) -> Digest:
    return Digest(
        chat_id=chat_id, thread_id=0, day=day, first_message_id=first, last_message_id=last,
        message_count=last - first + 1, summary="- s", model="m", stale=stale,
    )  # fmt: skip


@pytest.fixture
async def seeded(database: Database) -> Database:
    async with database.transaction() as session:
        session.add_all(
            [
                Chat(id=GROUP, type="supergroup", title="G"),
                Chat(id=PRIVATE, type="private"),
                Chat(id=SHORT, type="supergroup", title="S", retention_days=7),
            ]
        )
        await session.flush()
        session.add_all(
            [
                msg(GROUP, 1, days_ago=40),
                msg(GROUP, 2, days_ago=10, user_id=2),
                msg(GROUP, 3, days_ago=1, capture="invocation"),
                msg(PRIVATE, 1, days_ago=40, user_id=PRIVATE, capture="invocation"),
                msg(SHORT, 1, days_ago=10),
            ]
        )
        await session.flush()
        session.add_all(
            [
                MessageMedia(
                    chat_id=GROUP, message_id=1, bot_id=1, type="photo", file_id="f", file_unique_id="u"
                ),
                MessageMedia(
                    chat_id=GROUP, message_id=3, bot_id=1, type="photo", file_id="g", file_unique_id="v"
                ),
                Reaction(chat_id=GROUP, message_id=1, actor_id=2, reaction="🔥"),
                Reaction(chat_id=GROUP, message_id=2, actor_id=1, reaction="👍"),
                MemberEvent(chat_id=GROUP, user_id=1, kind="join", at=NOW - timedelta(days=40)),
                ChatMember(chat_id=GROUP, user_id=1, names=["Ann"]),
                digest(GROUP, (NOW - timedelta(days=400)).date(), 100, 110),
                digest(GROUP, (NOW - timedelta(days=40)).date(), 1, 1, stale=True),  # can't be rebuilt
                digest(GROUP, (NOW - timedelta(days=10)).date(), 2, 2, stale=True),  # can be rebuilt
                digest(GROUP, (NOW - timedelta(days=1)).date(), 3, 3),
            ]
        )
    return database


async def remaining(database: Database) -> set[tuple[int, int]]:
    async with database.session() as session:
        return {(r[0], r[1]) for r in await session.execute(select(Message.chat_id, Message.message_id))}


@pytest.mark.db
async def test_history_expires_per_chat_retention(seeded: Database) -> None:
    async with seeded.transaction() as session:
        assert await purge_expired(session, Retention(), NOW) == 2
    assert await remaining(seeded) == {(GROUP, 2), (GROUP, 3), (PRIVATE, 1)}
    async with seeded.session() as session:
        assert await session.scalar(select(func.count()).select_from(MessageMedia)) == 1
        assert list(await session.scalars(select(Reaction.message_id))) == [2]
        assert await session.scalar(select(func.count()).select_from(MemberEvent)) == 0
        days = sorted(d.last_message_id for d in await session.scalars(select(Digest)))
    assert days == [2, 3]


@pytest.mark.db
async def test_forgetting_a_member_scrubs_their_rows_in_that_chat(seeded: Database) -> None:
    async with seeded.transaction() as session:
        assert await forget_member(session, GROUP, 1, NOW) == 2
    async with seeded.session() as session:
        rows = {
            r.message_id: r for r in await session.scalars(select(Message).where(Message.chat_id == GROUP))
        }
        assert rows[1].text is None and rows[1].deleted_at is not None and rows[3].text is None
        assert rows[2].text == "m2" and rows[2].deleted_at is None
        assert await session.scalar(select(func.count()).select_from(MessageMedia)) == 0
        assert await session.scalar(select(func.count()).select_from(Reaction)) == 0  # theirs, and on theirs
        assert await session.scalar(select(func.count()).select_from(ChatMember)) == 0
        stale = {d.last_message_id: d.stale for d in await session.scalars(select(Digest))}
    assert stale[3] and stale[1] and stale[2]  # message 2's digest was already stale


@pytest.mark.db
async def test_context_off_purges_ambient_rows_and_digests(seeded: Database) -> None:
    async with seeded.transaction() as session:
        assert await purge_ambient(session, GROUP) == 2
    assert await remaining(seeded) == {(GROUP, 3), (PRIVATE, 1), (SHORT, 1)}
    async with seeded.session() as session:
        assert await session.scalar(select(func.count()).select_from(Digest)) == 0
        assert await session.scalar(select(func.count()).select_from(Reaction)) == 0
