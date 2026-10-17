"""Scheduling tools: reminders and scheduled prompts in this chat.

Anyone may schedule a one-off; in groups only admins may schedule repeating
jobs, members cancel their own jobs and admins any. A reminder just posts its
text; a scheduled prompt is answered by Derp when it fires, paid by its
creator's funding at that moment.

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
    JobAction,
    act_on_job,
    active_counts,
    has_offset,
    live_jobs,
    parse_local,
    show_time,
)
from derp.scheduler.models import Job

toolset = FunctionToolset[RunDeps]()


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
    if deps.trigger_message_id == 0:
        return "This is a scheduled task firing now; carry out the request instead of scheduling a new one."
    now = datetime.now(UTC)
    if at and not deps.chat.timezone and not has_offset(at):
        return (
            "This chat's time zone isn't set, so a wall-clock time can't be placed. Ask once where they are, "
            "set it with change_setting(time_zone, <IANA zone>), then schedule again. If they said UTC, give "
            "`at` with +00:00."
        )
    when = parse_local(at, deps.tz) if at else now + timedelta(minutes=in_minutes) if in_minutes else None
    if when is None:
        return "Give `at` (ISO local date-time) or `in_minutes`."
    if not now + MIN_AHEAD <= when <= now + MAX_AHEAD:
        return "The time must be between a minute and a year from now."
    if repeat and not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only admins can schedule repeating jobs in this group; one-off reminders are fine."
    async with deps.db.transaction() as session:
        total, mine = await active_counts(session, deps.chat.id, deps.user_id)
        if total >= CHAT_LIMIT or mine >= PERSON_LIMIT:
            return f"Too many scheduled jobs here (limit {CHAT_LIMIT} per chat, {PERSON_LIMIT} per person). Cancel some first."
        job = Job(
            chat_id=deps.chat.id,
            thread_id=deps.thread_id,
            creator_id=deps.user_id,
            kind="prompt" if answer_it else "message",
            text=text.strip()[:1000] or "⏰",
            repeat=repeat,
            next_at=when,
            anchor_at=when,
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    again = f", repeating {repeat}" if repeat else ""
    paid = " Each run is paid like a normal answer from their Derpcoins." if answer_it else ""
    return f"Scheduled #{job_id} for {show_time(when, deps.tz)}{again}.{paid} Show the time exactly as given."


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
            f"#{job.id} {show_time(job.next_at, deps.tz)}{repeat}{state} — {kind} by {one_line(name or 'someone')}: "
            f"{one_line(job.text, 120)}"
        )
    return "\n".join(lines)


async def _act(deps: RunDeps, job_id: int, action: JobAction) -> Job | str:
    """The job after ``action``, or what to tell the model instead."""
    admin = await is_admin(deps.bot, deps.chat, deps.user_id)
    async with deps.db.transaction() as session:
        outcome = await act_on_job(session, deps.chat, job_id, deps.user_id, admin, action)
    match outcome:
        case "gone":
            return "No such scheduled job here."
        case "not_yours":
            return f"Only its creator or an admin can {action} that one."
        case "passed":
            return "That one-off time has passed; schedule a new one instead."
        case job:
            return job


@toolset.tool
async def cancel_job(ctx: RunContext[RunDeps], job_id: int) -> str:
    """Cancel a scheduled job of this chat (your own, or any if you're an admin).

    Args:
        job_id: The #id from list_jobs.
    """
    outcome = await _act(ctx.deps, job_id, "cancel")
    return outcome if isinstance(outcome, str) else f"Cancelled #{job_id}."


@toolset.tool
async def resume_job(ctx: RunContext[RunDeps], job_id: int) -> str:
    """Resume a paused scheduled job of this chat (its creator or an admin).

    Args:
        job_id: The #id from list_jobs.
    """
    outcome = await _act(ctx.deps, job_id, "resume")
    if isinstance(outcome, str):
        return outcome
    return f"Resumed #{job_id}; next run {show_time(outcome.next_at, ctx.deps.tz)}."


family = Family("scheduler", toolset, deferred=("list_jobs", "cancel_job", "resume_job"))
