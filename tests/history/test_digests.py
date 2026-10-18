from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from derp.db.engine import Database
from derp.history.digests import (
    aimed_at,
    day_bounds,
    day_stats,
    is_current,
    last_said,
    latest_before,
    mark_stale,
    save,
    scope_key,
    span_messages,
)
from derp.history.models import Chat, ChatMember, Message, User
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


ASKER, OTHER = 11, 12


@pytest.mark.db
async def test_catching_up_starts_after_the_askers_last_message_before_others_spoke(
    database: Database,
) -> None:
    start = datetime(2026, 10, 10, 10, tzinfo=UTC)
    said = [
        ASKER,
        OTHER,
        ASKER,
        OTHER,
        OTHER,
        ASKER,
        ASKER,
    ]  # ids 1..7; 6 and 7 are a burst ending in the ask
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="D"))
        await session.flush()
        for n, sender in enumerate(said, 1):
            row = msg(n, start + timedelta(hours=n))
            row.user_id = sender
            session.add(row)
    scope = Scope(CHAT, tz=TZ)
    async with database.session() as session:
        anchor = await last_said(session, scope, ASKER, 7)
        assert anchor is not None and anchor.message_id == 3
        assert await last_said(session, scope, ASKER, 2) is None  # nobody else spoke before that
        until = day_bounds(DAY, TZ)[1]  # 15:00 UTC: the end of the 10th in Tokyo
        rows = await span_messages(session, scope, 3, 7, until)
        assert [r.message_id for r in rows] == [4]  # the rest of the anchor's chat day, and never the ask


@pytest.mark.db
async def test_lines_aimed_at_someone_are_replies_names_handles_and_tags(database: Database) -> None:
    at = datetime(2026, 10, 10, 10, tzinfo=UTC)
    texts = {
        1: (ASKER, "I'm off", None, None),
        2: (OTHER, "ok bye", 1, None),  # a reply to them
        3: (OTHER, "Александре напишите кто-нибудь?", None, None),  # their name, inflected
        4: (OTHER, "@sasha_k are you in?", None, None),
        5: (
            OTHER,
            "pinging you",
            None,
            [{"type": "text_mention", "offset": 0, "length": 7, "user": {"id": ASKER}}],
        ),
        6: (OTHER, "Саша, ты где?", None, None),  # a name she's had in the chat
        7: (ASKER, "Саша here, talking about myself", None, None),
        8: (None, "Саша, Derp says hi", None, None),
        9: (OTHER, "@sasha_kk is someone else", None, None),
        **dict.fromkeys(range(10, 30), (OTHER, "сашими или роллы?", None, None)),  # not her name
    }
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="D"))
        session.add_all(
            [User(id=ASKER, first_name="Александра", username="sasha_k"), User(id=OTHER, first_name="Lev")]
        )
        await session.flush()
        session.add(ChatMember(chat_id=CHAT, user_id=ASKER, names=["Саша К"]))
        for n, (sender, text, reply, entities) in texts.items():
            row = msg(n, at + timedelta(minutes=n))
            row.user_id, row.text, row.reply_to_message_id, row.entities = sender, text, reply, entities
            row.is_derp = sender is None
            session.add(row)
    async with database.session() as session:
        found = await aimed_at(
            session, Scope(CHAT), ASKER, start=at, end=None, after_id=1, before_id=99, limit=10
        )
        assert [r.message_id for r in found] == [2, 3, 4, 5, 6]
        newest = await aimed_at(  # twenty newer near-misses push nothing out
            session, Scope(CHAT), ASKER, start=at, end=None, after_id=1, before_id=99, limit=2
        )
        assert [r.message_id for r in newest] == [5, 6]
