"""Scheduling tools: reminders and scheduled prompts in this chat.

Anyone may schedule a one-off; in groups only admins may schedule repeating
jobs, members change and cancel their own jobs and admins any (an edit that
leaves a job repeating needs an admin too). A reminder just posts its
text; a scheduled prompt is answered by Derp when it fires, paid by its
creator's funding at that moment, so what it asks and how often stays the
creator's (``scheduler.jobs.may_touch``). A firing prompt neither schedules
nor edits jobs.

Wall-clock times are read in the chat's time zone. While a chat has none, a
wall-clock time is refused with a nudge to ask where they are and set it, since
a silent UTC guess fires at the wrong local hour; relative times and times with
an explicit offset need no zone.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.toolsets import Family
from derp.history.render import one_line
from derp.memory.notes import is_admin
from derp.scheduler.jobs import (
    CHAT_LIMIT,
    MAX_AHEAD,
    MIN_AHEAD,
    PERSON_LIMIT,
    Change,
    JobAction,
    act_on_job,
    has_offset,
    has_room,
    live_jobs,
    parse_local,
    show_time,
)
from derp.scheduler.models import Job

toolset = FunctionToolset[RunDeps]()
NO_ZONE = (
    "This chat's time zone isn't set, so a wall-clock time can't be placed. Ask once where they are, set it "
    "with change_setting(time_zone, <IANA zone>), then try again. If they said UTC, give `at` with +00:00."
)
FULL = f"Too many scheduled jobs here (limit {CHAT_LIMIT} per chat, {PERSON_LIMIT} per person). Cancel some first."


def _when(deps: RunDeps, at: str | None, in_minutes: int | None) -> datetime | str | None:
    """The time asked for (None when neither was given), or what to tell the model instead."""
    now = datetime.now(UTC)
    if at and not deps.chat.timezone and not has_offset(at):
        return NO_ZONE
    when = parse_local(at, deps.tz) if at else now + timedelta(minutes=in_minutes) if in_minutes else None
    if at and when is None:
        return "Give `at` as an ISO local date-time like 2026-10-20 09:00."
    if when is not None and not now + MIN_AHEAD <= when <= now + MAX_AHEAD:
        return "The time must be between a minute and a year from now."
    return when


def _firing(deps: RunDeps) -> str | None:
    """A scheduled prompt answering now (an id-0 invocation) carries the request out; it doesn't reschedule."""
    if deps.trigger_message_id == 0:
        return "This is a scheduled task firing now; carry out the request instead of scheduling or changing jobs."
    return None


def _text(text: str) -> str:
    return text.strip()[:1000] or "⏰"


def _repeats(job: Job) -> str:
    return f", repeating {job.repeat}" if job.repeat else ""


@toolset.tool
async def schedule(
    ctx: RunContext[RunDeps],
    text: str,
    at: str | None = None,
    in_minutes: int | None = None,
    repeat: Literal["daily", "weekdays", "weekly", "monthly"] | None = None,
    answer_it: bool = False,
) -> str:
    """Schedule a reminder (posts the text) or a prompt Derp answers at that time (answer_it).

    Args:
        text: The reminder text, or the request Derp should answer when it fires.
        at: Local date-time in the chat's time zone, ISO like 2026-10-20 09:00 (add +00:00 only if they
            said UTC).
        in_minutes: Alternatively, minutes from now.
        repeat: Repeat at the same local time (repeating jobs in groups need an admin).
        answer_it: True for "every morning, tell us the weather"-style requests.
    """
    deps = ctx.deps
    if refusal := _firing(deps):
        return refusal
    when = _when(deps, at, in_minutes)
    if when is None:
        return "Give `at` (ISO local date-time) or `in_minutes`."
    if isinstance(when, str):
        return when
    if repeat and not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only admins can schedule repeating jobs in this group; one-off reminders are fine."
    async with deps.db.transaction() as session:
        if not await has_room(session, deps.chat.id, deps.user_id):
            return FULL
        job = Job(
            chat_id=deps.chat.id,
            thread_id=deps.target.thread_id,
            creator_id=deps.user_id,
            kind="prompt" if answer_it else "message",
            text=_text(text),
            repeat=repeat,
            next_at=when,
            anchor_at=when,
        )
        session.add(job)
        await session.flush()
    paid = " Each run is paid like a normal answer from their Derpcoins." if answer_it else ""
    return f"Scheduled job {job.id} for {show_time(when, deps.tz)}{_repeats(job)}.{paid} Show the time exactly as given."


@toolset.tool
async def list_jobs(ctx: RunContext[RunDeps]) -> str:
    """Scheduled reminders and prompts in this chat."""
    deps = ctx.deps
    async with deps.db.session() as session:
        rows = await live_jobs(session, deps.chat.id)
    if not rows:
        return "Nothing is scheduled here."
    lines: list[str] = []
    for job, name in rows:
        state = " (paused)" if job.state == "paused" else ""
        repeat = f", {job.repeat}" if job.repeat else ""
        kind = "prompt" if job.kind == "prompt" else "reminder"
        lines.append(
            f"job {job.id} {show_time(job.next_at, deps.tz)}{repeat}{state} — {kind} by {one_line(name or 'someone')}: "
            f"{one_line(job.text, 120)}"
        )
    return "\n".join(lines)


async def _act(deps: RunDeps, job_id: int, action: JobAction | Change) -> Job | str:
    """The job after ``action``, or what to tell the model instead."""
    admin = await is_admin(deps.bot, deps.chat, deps.user_id)
    async with deps.db.transaction() as session:
        outcome = await act_on_job(session, deps.chat, job_id, deps.user_id, admin, action)
    match outcome:
        case "gone":
            return "No such scheduled job here."
        case "not_yours":
            return "Only its creator or an admin can change that one."
        case "creators":
            return (
                "A scheduled prompt runs on its creator's Derpcoins, so only they can make one, reword it or "
                "change how often it runs; admins can still move, pause or cancel it."
            )
        case "passed":
            return "That one-off's time has passed; give it a new time with edit_job."
        case "admins":
            return "Only admins can have a job repeat in this group; it can stay a one-off."
        case "full":
            return FULL
        case job:
            return job


@toolset.tool
async def cancel_job(ctx: RunContext[RunDeps], job_id: int) -> str:
    """Cancel a scheduled job of this chat (your own, or any if you're an admin).

    Args:
        job_id: The job number from list_jobs.
    """
    outcome = await _act(ctx.deps, job_id, "cancel")
    return outcome if isinstance(outcome, str) else f"Cancelled job {job_id}."


@toolset.tool
async def resume_job(ctx: RunContext[RunDeps], job_id: int) -> str:
    """Resume a paused scheduled job of this chat (its creator or an admin).

    Args:
        job_id: The job number from list_jobs.
    """
    outcome = await _act(ctx.deps, job_id, "resume")
    if isinstance(outcome, str):
        return outcome
    return f"Resumed job {job_id}; next run {show_time(outcome.next_at, ctx.deps.tz)}."


@toolset.tool
async def edit_job(
    ctx: RunContext[RunDeps],
    job_id: int,
    at: str | None = None,
    in_minutes: int | None = None,
    text: str | None = None,
    repeat: Literal["once", "daily", "weekdays", "weekly", "monthly"] | None = None,
    answer_it: bool | None = None,
) -> str:
    """Change a scheduled job of this chat in place: move it, reword it or change how it repeats.

    Args:
        job_id: The job number from schedule or list_jobs.
        at: A new local date-time, as in schedule; a paused job or a one-off that already fired comes back on.
        in_minutes: Alternatively, a new time in minutes from now.
        text: New reminder text or request; leave out to keep it.
        repeat: A new repeat rule at the job's local time, or "once" to stop repeating; leave out to keep it.
        answer_it: True to make it a prompt Derp answers, false a plain reminder; leave out to keep it.
    """
    deps = ctx.deps
    if refusal := _firing(deps):
        return refusal
    when = _when(deps, at, in_minutes)
    if isinstance(when, str):
        return when
    kind = None if answer_it is None else "prompt" if answer_it else "message"
    change = Change(at=when, text=_text(text) if text else None, kind=kind, repeat=repeat)
    if change == Change():
        return "Nothing to change: give a new time, text, repeat or answer_it."
    outcome = await _act(deps, job_id, change)
    if isinstance(outcome, str):
        return outcome
    paused = " It stays paused until resumed." if outcome.state == "paused" else ""
    return (
        f"Updated job {job_id}: next run {show_time(outcome.next_at, deps.tz)}{_repeats(outcome)}.{paused} "
        "Show the time exactly as given."
    )


family = Family("scheduler", toolset, deferred=("list_jobs", "edit_job", "cancel_job", "resume_job"))
