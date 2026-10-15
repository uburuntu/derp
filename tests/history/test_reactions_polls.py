from typing import Any

import pytest
from aiogram.types import Message as TgMessage
from aiogram.types import Poll as TgPoll
from aiogram.types import PollAnswer, ReactionTypeCustomEmoji, ReactionTypeEmoji, ReactionTypePaid
from sqlalchemy import select

from derp.db.engine import Database
from derp.history.capture import record_outgoing, upsert_chat
from derp.history.models import Poll, PollVote
from derp.history.polls import on_poll, on_poll_answer
from derp.history.reactions import reaction_key

GROUP = {"id": -100, "type": "supergroup", "title": "g"}
DERP = {"id": 777, "is_bot": True, "first_name": "Derp"}


def poll(*, votes: tuple[int, int] = (0, 0), closed: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "id": "quiz1", "question": "2+2?",
        "options": [
            {"text": "4", "voter_count": votes[0], "persistent_id": "o1"},
            {"text": "5", "voter_count": votes[1], "persistent_id": "o2"},
        ],
        "total_voter_count": sum(votes), "is_closed": closed, "is_anonymous": False, "type": "quiz",
        "allows_multiple_answers": False, "allows_revoting": False, "members_only": False,
        "correct_option_id": 0, **extra,
    }  # fmt: skip


def test_reaction_keys() -> None:
    assert reaction_key(ReactionTypeEmoji(emoji="🔥")) == "🔥"
    assert reaction_key(ReactionTypeCustomEmoji(custom_emoji_id="42")) == "custom:42"
    assert reaction_key(ReactionTypePaid()) == "paid"


@pytest.mark.db
async def test_derp_quiz_tracks_state_and_votes(database: Database) -> None:
    sent = TgMessage.model_validate({"message_id": 5, "date": 0, "chat": GROUP, "from": DERP, "poll": poll()})
    async with database.transaction() as session:
        await upsert_chat(session, sent.chat)
        await record_outgoing(session, [sent])
    await on_poll(TgPoll.model_validate(poll(votes=(2, 1), closed=True)), database)
    voter = {"id": 1, "is_bot": False, "first_name": "Ann"}
    await on_poll_answer(
        PollAnswer.model_validate(
            {"poll_id": "quiz1", "option_ids": [1], "option_persistent_ids": ["o2"], "user": voter}
        ),
        database,
    )
    await on_poll_answer(
        PollAnswer.model_validate(
            {"poll_id": "quiz1", "option_ids": [0], "option_persistent_ids": ["o1"], "user": voter}
        ),
        database,
    )
    await on_poll_answer(
        PollAnswer.model_validate(
            {"poll_id": "unknown", "option_ids": [0], "option_persistent_ids": ["o1"], "user": voter}
        ),
        database,
    )
    async with database.session() as session:
        row = await session.get(Poll, "quiz1")
        assert row is not None
        assert (row.origin, row.type, row.closed, row.total_voters) == ("derp_quiz", "quiz", True, 3)
        assert row.correct_option_ids == [0]
        assert [o["voters"] for o in row.options] == [2, 1]
        votes = (
            await session.execute(select(PollVote.poll_id, PollVote.voter_id, PollVote.option_ids))
        ).all()
        assert [tuple(v) for v in votes] == [("quiz1", 1, [0])]
