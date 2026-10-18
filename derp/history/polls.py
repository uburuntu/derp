"""Polls and votes.

What the Bot API lets Derp know: every poll message it captures (question and
options as posted), live state only for its own polls and polls someone
stopped, and votes only in its own non-anonymous polls. Updates for polls
that were never captured are ignored, since they carry no chat.

A member's first answer to one of Derp's quizzes scores the game: +1 when
right, −1 when wrong. A chat-local day's points are their plain sum, so the
order of answers doesn't matter; readers count a day as at least zero (quiz
answers can't be changed, and a redelivered answer scores nothing).
"""

from datetime import UTC, datetime
from typing import Any

from aiogram import Router
from aiogram.types import Poll as TgPoll
from aiogram.types import PollAnswer
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.db.engine import Database
from derp.history.models import Chat, GameScore, Poll, PollVote, User
from derp.history.render import chat_time


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
        poll = await session.get(Poll, answer.poll_id, with_for_update=True)  # one answer at a time per poll
        if poll is None:
            return
        first = await session.get(PollVote, (answer.poll_id, voter)) is None
        if (
            first
            and answer.user
            and answer.option_ids
            and poll.origin == "derp_quiz"
            and poll.correct_option_ids
        ):
            await session.execute(  # the leaderboard needs a name for voters who never wrote here
                insert(User)
                .values(
                    id=voter,
                    is_bot=answer.user.is_bot,
                    first_name=answer.user.first_name,
                    last_name=answer.user.last_name,
                    username=answer.user.username,
                )
                .on_conflict_do_nothing(index_elements=[User.id])
            )
            await score(
                session, poll, voter, right=sorted(answer.option_ids) == sorted(poll.correct_option_ids)
            )
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


async def score(session: AsyncSession, poll: Poll, user_id: int, *, right: bool) -> None:
    day = datetime.now(UTC).astimezone(chat_time(await session.get(Chat, poll.chat_id))).date()
    delta = 1 if right else -1
    statement = insert(GameScore).values(
        chat_id=poll.chat_id, user_id=user_id, day=day, points=delta, answers=1
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[GameScore.chat_id, GameScore.user_id, GameScore.day],
            set_={"points": GameScore.points + delta, "answers": GameScore.answers + 1},
        )
    )


def build_router() -> Router:
    router = Router(name="polls")
    router.poll.register(on_poll)
    router.poll_answer.register(on_poll_answer)
    return router
