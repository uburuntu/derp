from datetime import UTC, datetime, timedelta

import pytest

from derp.db.engine import Database
from derp.history.models import Chat, ChatMember, MemberEvent, Message, Poll, Reaction, ReactionCount, User
from derp.history.queries import ChatQuery, Scope, around, resolve_members, run_query, search

CHAT = -700
NOW = datetime.now(UTC)


def msg(
    message_id: int, user_id: int, text: str, *, days_ago: float = 0, thread_id: int | None = None
) -> Message:
    return Message(
        chat_id=CHAT, message_id=message_id, user_id=user_id, thread_id=thread_id,
        date=NOW - timedelta(days=days_ago), kind="text", text=text, capture="ambient", payload={},
    )  # fmt: skip


@pytest.fixture
async def seeded(database: Database) -> Database:
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="Q"))
        session.add_all(
            [User(id=1, first_name="Ann", username="annie"), User(id=2, first_name="Bob", last_name="Stone")]
        )
        session.add_all(
            [
                ChatMember(chat_id=CHAT, user_id=1, names=["Anya", "Ann"]),
                ChatMember(chat_id=CHAT, user_id=2, names=["Bob Stone"]),
            ]
        )
        session.add_all(
            [
                msg(1, 1, "pizza tonight again?", days_ago=10),
                msg(2, 2, "закажем пиццу", days_ago=2),
                msg(3, 2, "I love pizza", days_ago=1),
                msg(4, 1, "topic chatter", thread_id=5),
                msg(5, 1, "meh"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Reaction(chat_id=CHAT, message_id=3, actor_id=1, reaction="🔥"),
                Reaction(chat_id=CHAT, message_id=3, actor_id=1, reaction="👍"),
                ReactionCount(chat_id=CHAT, message_id=1, reaction="😁", count=5),
                MemberEvent(chat_id=CHAT, user_id=2, kind="join"),
                Poll(
                    id="p", chat_id=CHAT, message_id=9, question="Pizza?", options=[{"id": "a", "text": "yes", "voters": 0}],
                    type="regular", is_anonymous=True, multiple=False, origin="user",
                ),
            ]
        )  # fmt: skip
    return database


@pytest.mark.db
async def test_search_ranks_full_text_keeps_stop_words_and_falls_back_to_fuzzy(seeded: Database) -> None:
    async with seeded.session() as session:
        whole = Scope(CHAT)
        assert [m.message_id for m in await search(session, whole, "again")] == [1]
        assert {m.message_id for m in await search(session, whole, "pizza")} == {1, 3}
        assert [m.message_id for m in await search(session, whole, "пиццу")] == [2]
        assert 3 in {m.message_id for m in await search(session, whole, "pizzza")}  # typo → trigram
        recent = await search(session, whole, "pizza", since=NOW - timedelta(days=5))
        assert [m.message_id for m in recent] == [3]
        general = Scope(CHAT, None, whole_chat=False)
        assert await search(session, general, "chatter") == []


@pytest.mark.db
async def test_members_resolve_by_name_old_name_and_username(seeded: Database) -> None:
    async with seeded.session() as session:
        assert await resolve_members(session, CHAT, ["@annie"]) == [1]
        assert await resolve_members(session, CHAT, ["anya"]) == [1]
        assert await resolve_members(session, CHAT, ["bob stone"]) == [2]
        assert await resolve_members(session, CHAT, ["zed"]) == []


@pytest.mark.db
async def test_presets_and_filters(seeded: Database) -> None:
    async with seeded.session() as session:
        scope = Scope(CHAT)
        activity = await run_query(session, scope, ChatQuery(preset="member_activity"))
        assert activity.groups == [("Ann", 3), ("Bob Stone", 2)]
        leaders = await run_query(session, scope, ChatQuery(preset="reaction_leaderboard"))
        assert leaders.groups[0] == ("Ann", 5)  # anonymous totals count when present
        top = await run_query(session, scope, ChatQuery(preset="top_messages"))
        assert [m.message_id for m in top.messages] == [1, 3]
        bob = await run_query(session, scope, ChatQuery(users=["Bob"], contains="PIZZA"))
        assert [m.message_id for m in bob.messages] == [3]
        fire = await run_query(session, scope, ChatQuery(reaction="🔥"))
        assert [m.message_id for m in fire.messages] == [3]
        by_reaction = await run_query(session, scope, ChatQuery(group_by="reaction"))
        assert dict(by_reaction.groups) == {"🔥": 1, "👍": 1}
        events = await run_query(session, scope, ChatQuery(preset="joins_leaves"))
        assert [(e.kind, name) for e, name in events.events] == [("join", "Bob Stone")]
        polls = await run_query(session, scope, ChatQuery(preset="poll_results"))
        assert [p.question for p in polls.polls] == ["Pizza?"]
        nobody = await run_query(session, scope, ChatQuery(users=["zed"]))
        assert nobody.messages == []


@pytest.mark.db
async def test_around_stays_in_the_anchor_topic(seeded: Database) -> None:
    async with seeded.session() as session:
        rows = await around(session, CHAT, 3, before=5, after=5)
        assert [m.message_id for m in rows] == [1, 2, 3, 5]
