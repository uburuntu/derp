"""Polls and votes.

What the Bot API lets Derp know: every poll message it captures (question and
options as posted), live state only for its own polls and polls someone
stopped, and votes only in its own non-anonymous polls. Updates for polls
that were never captured are ignored, since they carry no chat.
"""

from typing import Any

from aiogram import Router
from aiogram.types import Poll as TgPoll
from aiogram.types import PollAnswer
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.db.engine import Database
from derp.history.models import Poll, PollVote


def poll_state(poll: TgPoll) -> dict[str, Any]:
    correct = poll.correct_option_ids
    if correct is None and poll.correct_option_id is not None:
        correct = [poll.correct_option_id]
    return {
        "question": poll.question,
        "options": [
            {"id": option.persistent_id, "text": option.text, "voters": option.voter_count}
            for option in poll.options
        ],
        "total_voters": poll.total_voter_count,
        "correct_option_ids": correct,
        "explanation": poll.explanation,
        "closed": poll.is_closed,
        "close_date": poll.close_date,
    }


async def upsert_poll(
    session: AsyncSession, poll: TgPoll, *, chat_id: int, message_id: int, by_derp: bool
) -> None:
    kind = "quiz" if poll.type == "quiz" else "regular"
    origin = ("derp_quiz" if kind == "quiz" else "derp_poll") if by_derp else "user"
    state = poll_state(poll)
    statement = insert(Poll).values(
        id=poll.id,
        chat_id=chat_id,
        message_id=message_id,
        type=kind,
        is_anonymous=poll.is_anonymous,
        multiple=poll.allows_multiple_answers,
        origin=origin,
        **state,
    )
    await session.execute(statement.on_conflict_do_update(index_elements=[Poll.id], set_=state))


async def on_poll(poll: TgPoll, db: Database) -> None:
    async with db.transaction() as session:
        row = await session.get(Poll, poll.id)
        if row is None:
            return
        for key, value in poll_state(poll).items():
            setattr(row, key, value)


async def on_poll_answer(answer: PollAnswer, db: Database) -> None:
    voter = answer.user.id if answer.user else answer.voter_chat.id if answer.voter_chat else None
    if voter is None:
        return
    async with db.transaction() as session:
        if await session.get(Poll, answer.poll_id) is None:
            return
        statement = insert(PollVote).values(
            poll_id=answer.poll_id,
            voter_id=voter,
            option_ids=answer.option_ids,
            option_persistent_ids=answer.option_persistent_ids,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[PollVote.poll_id, PollVote.voter_id],
                set_={
                    "option_ids": statement.excluded.option_ids,
                    "option_persistent_ids": statement.excluded.option_persistent_ids,
                    "voted_at": func.now(),
                },
            )
        )


def build_router() -> Router:
    router = Router(name="polls")
    router.poll.register(on_poll)
    router.poll_answer.register(on_poll_answer)
    return router
