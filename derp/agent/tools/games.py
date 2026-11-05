"""Chat games: polls and scored quizzes, and titles of the day.

One poll or a series goes out as one output (its later polls ride in the first one's slot:
``outputs.deliver``). Quizzes are Derp's own non-anonymous quiz polls, so answers arrive as
``poll_answer`` updates and score the chat's quiz leaderboard (see ``derp.history.polls``; read it with
the ``quiz_leaderboard`` chat query); Telegram shows their options shuffled. An option that is exactly a
member's name carries their profile photo, and members may add options to regular polls that show voters.
Titles of the day are drawn and kept by ``derp.history.titles``.
"""

from collections.abc import Awaitable
from datetime import UTC, datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto, InputPollOption
from aiogram.types import Message as TgMessage
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.outputs import Refused, Unconfirmed, check_budget, deliver, unconfirmed
from derp.agent.tools.telegram import reply_parameters
from derp.agent.toolsets import Family
from derp.history.models import User
from derp.history.queries import members_named
from derp.history.titles import crown, leaderboard, normalize
from derp.telegram.media import MediaUnavailableError, profile_photo_size

toolset = FunctionToolset[RunDeps]()
MAX_POLLS = 10
AVATAR_SIDE = 160  # an option's photo is a thumbnail
DAY_MINUTES = 24 * 60
MAX_MINUTES_OPEN = 30 * DAY_MINUTES  # Telegram closes a poll at most 2628000 s (about a month) out


class Poll(BaseModel):
    question: str = Field(max_length=300)
    options: list[str] = Field(min_length=2, max_length=12, description="Up to 100 characters each.")
    correct: int | None = Field(default=None, ge=0, description="Index of the right option: a scored quiz.")
    explanation: str | None = Field(
        default=None, max_length=200, description="Quiz only: shown after answering."
    )
    multiple_answers: bool = Field(default=False, description="Regular polls only.")


async def _options(deps: RunDeps, options: list[str]) -> list[InputPollOption]:
    """The options, each that is exactly a member's name with their profile photo (when they show one)."""
    async with deps.db.session() as session:
        named = await members_named(session, deps.chat.id, options)
    return [
        InputPollOption(text=option[:100], media=await _face(deps, named.get(option))) for option in options
    ]


async def _face(deps: RunDeps, user_id: int | None) -> InputMediaPhoto | None:
    if user_id is None:
        return None
    try:
        return InputMediaPhoto(media=(await profile_photo_size(deps.bot, user_id, AVATAR_SIDE)).file_id)
    except MediaUnavailableError:
        return None


@toolset.tool(sequential=True)
async def post_polls(
    ctx: RunContext[RunDeps],
    polls: list[Poll],
    minutes_open: int = DAY_MINUTES,
    anonymous: bool = False,
    blind: bool = False,
) -> str:
    """Post a poll, or a series of up to 10 as one output: regular polls and/or quizzes (with `correct`).

    Quizzes score the chat's quiz leaderboard (+1 right, −1 wrong, daily floor 0). Write the questions
    yourself (a quiz about the chat: from its history). "Who's most likely to…" polls take members' names
    as options (they show with their profile photos).

    Args:
        polls: The polls, in order.
        minutes_open: How long each stays open, 1–43200. People drop in later, so a day fits unless it's a
            live game or they asked.
        anonymous: Hide who voted (regular polls; quizzes are never anonymous).
        blind: Show the results only once it closes.
    """
    deps = ctx.deps
    try:
        check_budget(deps)
        if not 1 <= len(polls) <= MAX_POLLS:
            raise Refused(f"Not posted: 1–{MAX_POLLS} polls at a time.")
        for number, item in enumerate(polls, 1):
            if item.correct is not None and item.correct >= len(item.options):
                raise Refused(f"Not posted: poll {number}: correct must point at one of its options.")
    except Refused as refused:
        return str(refused)
    period = max(1, min(minutes_open, MAX_MINUTES_OPEN)) * 60
    hidden = anonymous and deps.chat.type != "private"
    posted: list[int] = []
    unsure = False
    for item in polls:
        quiz = item.correct is not None
        options = await _options(deps, item.options)

        async def send(
            item: Poll = item, quiz: bool = quiz, options: list[InputPollOption] = options
        ) -> TgMessage:
            def poll(options: list[InputPollOption]) -> Awaitable[TgMessage]:
                return deps.bot.send_poll(
                    chat_id=deps.chat.id,
                    message_thread_id=deps.target.thread_id,
                    question=item.question[:300],
                    options=[*options],
                    type="quiz" if quiz else "regular",
                    is_anonymous=hidden and not quiz,
                    allows_multiple_answers=item.multiple_answers and not quiz,
                    shuffle_options=quiz or None,  # the right answer lands anywhere
                    allow_adding_options=not (quiz or hidden) or None,
                    hide_results_until_closes=blind or None,
                    correct_option_ids=[item.correct] if item.correct is not None else None,
                    explanation=item.explanation[:200] if item.explanation and quiz else None,
                    open_period=period,
                    reply_parameters=reply_parameters(deps.target.reply_to) if not posted else None,
                )

            try:
                return await poll(options)
            except TelegramBadRequest:  # a photo Telegram won't put on an option: rejected, so safe to resend
                if all(option.media is None for option in options):
                    raise
                return await poll([option.model_copy(update={"media": None}) for option in options])

        try:
            [sent] = await deliver(deps, "poll", "poll", send, rides=bool(posted))
        except Unconfirmed as refused:
            if not posted:
                return f"{refused} Don't post the rest either."
            unsure = True
            break
        except Refused as refused:
            if not posted:
                return str(refused)
            break
        posted.append(sent.message_id)
    ids = ", ".join(f"#{message_id}" for message_id in posted)
    note = (
        " Quiz points count on the leaderboard; don't reveal the answers."
        if any(p.correct is not None for p in polls)
        else ""
    )
    if unsure:
        note += f" {unconfirmed('next poll')} Don't post the rest either."
    return f"Posted {len(posted)} of {len(polls)} ({ids}).{note}"


@toolset.tool
async def title_of_the_day(ctx: RunContext[RunDeps], title: str) -> str:
    """Name today's "<title> of the day" (e.g. красавчик дня) at random among the group's active members.

    Asked again today, the same title gives the same winner. Announce it in your own words.

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


family = Family("games", toolset, deferred=("title_of_the_day",))
