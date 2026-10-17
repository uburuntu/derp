from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from derp.db.engine import Database
from derp.history.digests import day_stats, is_current, latest_before, mark_stale, save, scope_key
from derp.history.models import Chat, Message
from derp.history.queries import Scope

CHAT = -710
TZ = ZoneInfo("Asia/Tokyo")  # UTC+9: the chat day starts at 15:00 UTC the day before
DAY = date(2026, 10, 10)


def msg(message_id: int, at: datetime, *, thread_id: int | None = None) -> Message:
    return Message(
        chat_id=CHAT, message_id=message_id, thread_id=thread_id, date=at, kind="text",
        text=f"m{message_id}", capture="ambient", payload={},
    )  # fmt: skip


@pytest.fixture
async def seeded(database: Database) -> Database:
    start = datetime(2026, 10, 9, 15, tzinfo=UTC)  # midnight in Tokyo
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="D", is_forum=True))
        await session.flush()
        session.add_all(
            [
                msg(1, start - timedelta(minutes=1)),  # the previous chat day
                msg(2, start),
                msg(3, start + timedelta(hours=5), thread_id=7),
                msg(4, start + timedelta(hours=23, minutes=59)),
                msg(5, start + timedelta(days=1)),  # the next chat day
            ]
        )
    return database


@pytest.mark.db
async def test_day_stats_follow_the_chat_time_zone_and_scope(seeded: Database) -> None:
    whole, topic = Scope(CHAT, tz=TZ), Scope(CHAT, 7, whole_chat=False, tz=TZ)
    assert (scope_key(whole), scope_key(topic)) == (0, 7)
    async with seeded.session() as session:
        stats = await day_stats(session, whole, DAY)
        assert stats is not None and (stats.first_id, stats.last_id, stats.count) == (2, 4, 3)
        in_topic = await day_stats(session, topic, DAY)
        assert in_topic is not None and in_topic.count == 1
        assert await day_stats(session, whole, DAY - timedelta(days=5)) is None


@pytest.mark.db
async def test_edits_inside_a_digest_mark_it_stale(seeded: Database) -> None:
    async with seeded.transaction() as session:
        stats = await day_stats(session, Scope(CHAT, tz=TZ), DAY)
        assert stats is not None
        digest = await save(session, CHAT, 0, DAY, stats, "- things", "m")
        assert is_current(digest, stats)
        await mark_stale(session, CHAT, 5)  # outside the day: untouched
        await session.refresh(digest)
        assert not digest.stale
        assert await latest_before(session, CHAT, 0, 5) is digest
        assert await latest_before(session, CHAT, 0, 4) is None  # overlaps the window
        await mark_stale(session, CHAT, 3)
        await session.refresh(digest)
        assert digest.stale and not is_current(digest, stats)
        assert await latest_before(session, CHAT, 0, 5) is None
