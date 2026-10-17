"""Chat games: poll series with scored quizzes, "who's most likely to…" polls and titles of the day.

A series mixes regular polls and quizzes. Quizzes are Derp's own non-anonymous
quiz polls, so answers arrive as ``poll_answer`` updates and score the chat's
quiz leaderboard (see ``derp.history.polls``; read it with the
``quiz_leaderboard`` chat query). A whole series takes one output slot however
many polls it has. Titles of the day are drawn and kept by ``derp.history.titles``.
"""

from datetime import UTC, datetime

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputPollOption
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.tools.telegram import (
    OutputRefused,
    check_budget,
    delivered,
    reply_parameters,
    unconfirmed,
)
from derp.agent.toolsets import Family
from derp.history.models import User
from derp.history.queries import ChatQuery, Scope, run_query
from derp.history.titles import crown, leaderboard, normalize
from derp.telegram.policy import AMBIGUOUS

toolset = FunctionToolset[RunDeps]()
MAX_POLLS = 5
ACTIVE_DAYS = 30


class SeriesPoll(BaseModel):
    question: str = Field(max_length=300)
    options: list[str] = Field(min_length=2, max_length=10, description="Up to 100 characters each.")
    correct: int | None = Field(
        default=None, ge=0, description="0-based index of the right option; makes it a scored quiz."
    )
    explanation: str | None = Field(
        default=None, max_length=200, description="Quiz only: shown after answering."
    )
    multiple_answers: bool = Field(default=False, description="Regular polls only: allow several choices.")


@toolset.tool(sequential=True)
async def post_polls(
    ctx: RunContext[RunDeps], polls: list[SeriesPoll], seconds_per_poll: int | None = None
) -> str:
    """Post a series of up to 5 polls: quizzes (with `correct`) and/or regular polls, all non-anonymous.

    Quiz answers score the chat's quiz leaderboard: +1 right, −1 wrong (daily floor 0). Write the questions
    yourself, on a topic or from this chat's history (search it first); keep options short and vary the
    position of the right one. For a single regular poll, send_poll is enough.

    Args:
        polls: 1–5 polls.
        seconds_per_poll: Close each poll after 5–600 seconds (default: stays open).
    """
    deps = ctx.deps
    try:
        check_budget(deps)
        if not 1 <= len(polls) <= MAX_POLLS:
            raise OutputRefused(f"a series has 1–{MAX_POLLS} polls")
        for number, item in enumerate(polls, 1):
            if item.correct is not None and item.correct >= len(item.options):
                raise OutputRefused(f"poll {number}: correct must point at one of its options")
    except OutputRefused as refused:
        return f"Not posted: {refused}."
    period = max(5, min(seconds_per_poll, 600)) if seconds_per_poll else None
    posted: list[int] = []
    unsure = False
    for item in polls:
        quiz = item.correct is not None
        try:
            sent = await deps.bot.send_poll(
                chat_id=deps.chat.id,
                message_thread_id=deps.thread_id,
                question=item.question[:300],
                options=[InputPollOption(text=option[:100]) for option in item.options],
                type="quiz" if quiz else "regular",
                is_anonymous=False,
                allows_multiple_answers=item.multiple_answers and not quiz,
                correct_option_ids=[item.correct] if item.correct is not None else None,
                explanation=item.explanation[:200] if item.explanation and quiz else None,
                open_period=period,
                reply_parameters=reply_parameters(deps.target.reply_to) if not posted else None,
            )
        except AMBIGUOUS:
            if not posted:
                return unconfirmed("poll") + " Don't post the rest of the series either."
            unsure = True
            break
        except TelegramAPIError as error:
            if not posted:
                return f"Telegram refused the poll ({type(error).__name__})."
            break
        await delivered(deps, sent, "poll")
        posted.append(sent.message_id)
    ids = ", ".join(f"#{message_id}" for message_id in posted)
    note = (
        " Quiz points count on the quiz leaderboard; don't reveal the answers."
        if any(p.correct is not None for p in polls)
        else ""
    )
    if unsure:
        note += f" {unconfirmed('next poll')} Don't post the rest either."
    return f"Posted {len(posted)} of {len(polls)} polls ({ids}).{note}"


@toolset.tool(sequential=True)
async def most_likely_poll(ctx: RunContext[RunDeps], question: str) -> str:
    """A "who's most likely to…" poll with this chat's most active members as the options.

    Args:
        question: Like "Who's most likely to oversleep the exam?"
    """
    deps = ctx.deps
    if deps.chat.type == "private":
        return "That needs a group."
    try:
        check_budget(deps)
    except OutputRefused as refused:
        return f"Not sent: {refused}."
    query = ChatQuery(preset="member_activity", days=ACTIVE_DAYS, whole_chat=True, limit=10)
    async with deps.db.session() as session:
        result = await run_query(session, Scope(deps.chat.id), query)
    names = list(dict.fromkeys(label[:100] for label, _count in result.groups if label != "anonymous"))
    if len(names) < 2:
        return "Not enough active members in the stored history for that poll."
    try:
        sent = await deps.bot.send_poll(
            chat_id=deps.chat.id,
            message_thread_id=deps.thread_id,
            question=question[:300],
            options=[InputPollOption(text=name) for name in names],
            is_anonymous=False,
            reply_parameters=reply_parameters(deps.target.reply_to),
        )
    except AMBIGUOUS:
        return unconfirmed("poll")
    except TelegramAPIError as error:
        return f"Telegram refused the poll ({type(error).__name__})."
    await delivered(deps, sent, "poll")
    return f"Poll posted as #{sent.message_id} with {len(names)} members."


@toolset.tool
async def title_of_the_day(ctx: RunContext[RunDeps], title: str) -> str:
    """Name today's "<title> of the day" (e.g. красавчик дня), drawn at random among the group's active members.

    The same title asked again today gives the same winner. Announce it in your own words.

    Args:
        title: The title alone, in the chat's language (e.g. "красавчик").
    """
    deps = ctx.deps
    if deps.chat.type == "private":
        return "Titles of the day are a group game."
    key = normalize(title)
    if not key:
        return "Give the title a name."
    now = datetime.now(UTC)
    async with deps.db.transaction() as session:
        picked = await crown(session, deps.chat.id, key, now.astimezone(deps.tz).date(), now)
        if picked is None:
            return "Nobody has been active here lately to pick from."
        user_id, fresh = picked
        winner = await session.get(User, user_id)
        board = await leaderboard(session, deps.chat.id, title=key, limit=5)
    name = " ".join(p for p in (winner.first_name, winner.last_name) if p) if winner else "someone"
    handle = f" (@{winner.username})" if winner and winner.username else ""
    when = "just drawn" if fresh else "already drawn earlier today"
    wins = "; ".join(f"{who} {count}" for _title, who, count in board)
    return f"{key} of the day: {name}{handle}, {when}. All-time: {wins}."


family = Family("games", toolset, deferred=True)
