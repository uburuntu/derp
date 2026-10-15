from datetime import UTC, datetime

from sqlalchemy import select, update

from derp.history.models import Chat, Poll, Reaction, ReactionCount
from tests.e2e.harness import Conversation, Person, Room

GROUP = Room(-1003, title="Pollsters")
ANN = Person(401, "Ann")
BOB = Person(402, "Bob")


def reaction(message_id: int, person: Person, *emoji: str) -> dict[str, object]:
    return {
        "chat": GROUP.payload(),
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "user": person.payload(),
        "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": e} for e in emoji],
    }


async def reactions(conversation: Conversation) -> set[tuple[int, int, str]]:
    async with conversation.db.session() as session:
        rows = await session.execute(select(Reaction.message_id, Reaction.actor_id, Reaction.reaction))
        return {tuple(row) for row in rows}  # type: ignore[misc]


async def test_reactions_follow_stored_messages(conversation: Conversation) -> None:
    conversation.meet(ANN, GROUP)
    conversation.meet(BOB, GROUP)
    hello = await conversation.send(ANN, GROUP, "hello all")
    await conversation.settle(hello)

    await conversation.settle(
        await conversation.push("message_reaction", reaction(hello.message_id, BOB, "🔥", "👍"))
    )
    assert await reactions(conversation) == {
        (hello.message_id, BOB.id, "🔥"),
        (hello.message_id, BOB.id, "👍"),
    }
    await conversation.settle(
        await conversation.push("message_reaction", reaction(hello.message_id, BOB, "😁"))
    )
    assert await reactions(conversation) == {(hello.message_id, BOB.id, "😁")}

    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == GROUP.id).values(ambient=False))
    quiet = await conversation.send(ANN, GROUP, "not stored")
    await conversation.settle(quiet)
    await conversation.settle(
        await conversation.push("message_reaction", reaction(quiet.message_id, BOB, "🔥"))
    )
    assert (quiet.message_id, BOB.id, "🔥") not in await reactions(conversation)

    counts = {
        "chat": GROUP.payload(),
        "message_id": hello.message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "reactions": [{"type": {"type": "emoji", "emoji": "😁"}, "total_count": 3}],
    }
    await conversation.settle(await conversation.push("message_reaction_count", counts))
    async with conversation.db.session() as session:
        count = await session.get(ReactionCount, (GROUP.id, hello.message_id, "😁"))
        assert count is not None and count.count == 3


async def test_user_poll_is_captured_as_posted(conversation: Conversation) -> None:
    conversation.meet(ANN, GROUP)
    posted = await conversation.send(
        ANN,
        GROUP,
        poll={
            "id": "user-poll", "question": "Pizza?",
            "options": [{"text": "yes", "voter_count": 0, "persistent_id": "a"},
                        {"text": "no", "voter_count": 0, "persistent_id": "b"}],
            "total_voter_count": 0, "is_closed": False, "is_anonymous": True, "type": "regular",
            "allows_multiple_answers": False, "allows_revoting": True, "members_only": False,
        },
    )  # fmt: skip
    await conversation.settle(posted)
    async with conversation.db.session() as session:
        row = await session.get(Poll, "user-poll")
        assert row is not None
        assert (row.origin, row.chat_id, row.message_id) == ("user", GROUP.id, posted.message_id)
        assert [o["text"] for o in row.options] == ["yes", "no"]
