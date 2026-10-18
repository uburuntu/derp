"""Job rules: who may schedule and touch what, when it fires next, and how times are shown.

Repeats keep the first occurrence's wall-clock time in the chat's time zone (so
09:00 stays 09:00 across daylight-saving changes, and a time skipped by a
spring-forward gap returns the next day) and its day of month (a job on the
31st fires on the last day of shorter months, then on the 31st again).
Firings missed while Derp was down are skipped, not replayed: a job fires at
most once per claim.

A one-off holds an absolute instant, so changing the chat's time zone never
moves it; repeating jobs are re-anchored to keep their wall-clock time in the
new zone (``rezone``). Chats without a zone run on UTC, but their jobs were
placed as instants (relative times, explicit offsets), so a first zone keeps
those instants too.

Creators pause, resume, change and cancel their own jobs, admins any
(``act_on_job``, behind the agent's tools, the /settings jobs page and the
Resume button), except that what a prompt is and how often it runs stays its
creator's (``may_touch``): every firing is paid from the creator's wallet. A job
that repeats after the change needs an admin in groups. Giving a job a new time
schedules it there: a paused job or a finished one-off comes back on; a new
repeat alone moves it to that rule's first occurrence. A fired reminder's Snooze
buttons do the same for a one-off; a repeating one keeps its series and gets one
extra firing, which pauses, resumes and is cancelled with it. The worker claims due
rows with ``SKIP LOCKED`` and every action locks its row, so a change racing a
firing lands either before the claim (the job fires on the new terms) or after
it (on top of the moved-on row).
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.models import Chat, User
from derp.history.render import chat_time
from derp.scheduler.models import Job

MAX_AHEAD = timedelta(days=366)
MIN_AHEAD = timedelta(seconds=30)
CHAT_LIMIT = 25  # active jobs per chat
PERSON_LIMIT = 10  # active jobs per person per chat
MISS_LIMIT = 3  # unfunded firings in a row before a job pauses
LIVE = ("active", "paused")  # jobs still scheduled; done and cancelled ones only await deletion

type JobAction = Literal["pause", "resume", "cancel"]
type Refusal = Literal["gone", "not_yours", "creators", "passed", "admins", "full", "fired"]
SNOOZES = ("10m", "1h", "1d")  # the Snooze buttons on a fired reminder: +10 minutes, +1 hour, tomorrow


@dataclass(frozen=True, slots=True)
class Change:
    """An edit to a job in place; fields left ``None`` stay as they are."""

    at: datetime | None = None  # a new time: the job is scheduled there (and comes back on)
    text: str | None = None
    kind: Literal["message", "prompt"] | None = None
    repeat: Literal["once", "daily", "weekdays", "weekly", "monthly"] | None = None  # once: stop repeating


@dataclass(frozen=True, slots=True)
class Snooze:
    """Fire a reminder again at ``until``.

    A one-off moves there; a repeating job keeps its series and gets one extra firing (``Job.snooze_of``),
    which a later snooze moves instead of adding another.
    """

    until: datetime
    run: int  # the firing whose button was pressed: once the job fired again, that button is stale


class SnoozeJob(CallbackData, prefix="jobsnooze"):
    """A Snooze button on a fired reminder (handled with the jobs page in ``settings/panel.py``)."""

    id: int
    run: int  # Job.runs as it fired
    by: str  # one of SNOOZES


def snoozed_until(by: str, now: datetime, tz: tzinfo) -> datetime:
    match by:
        case "10m":
            return now + timedelta(minutes=10)
        case "1h":
            return now + timedelta(hours=1)
        case _:  # tomorrow at this local time
            return following(now, "daily", tz)


class ResumeJob(CallbackData, prefix="jobresume"):
    """The Resume button on a paused job's note (handled with the jobs page in ``settings/panel.py``)."""

    id: int


def _add_month(day: date, wanted: int) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(wanted, calendar.monthrange(year, month)[1]))


def following(at: datetime, repeat: str, tz: tzinfo, anchor: datetime | None = None) -> datetime:
    """The occurrence after ``at`` for a repeat rule, at the anchor's local wall time and day of month."""
    first = (anchor or at).astimezone(tz)
    day = at.astimezone(tz).date()
    match repeat:
        case "daily":
            day += timedelta(days=1)
        case "weekdays":
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
        case "weekly":
            day += timedelta(days=7)
        case _:
            day = _add_month(day, first.day)
    return datetime.combine(day, first.time(), tz).astimezone(UTC)


def first_after(now: datetime, repeat: str, tz: tzinfo, anchor: datetime) -> datetime:
    """The first occurrence of a repeat rule after ``now``, counted from the anchor (a weekday rule skips a
    weekend anchor): where a job lands when only its repeat changes."""
    upcoming = anchor
    while upcoming <= now or (repeat == "weekdays" and upcoming.astimezone(tz).weekday() >= 5):
        upcoming = following(upcoming, repeat, tz, anchor)
    return upcoming


def next_after(job: Job, now: datetime, tz: tzinfo) -> datetime | None:
    """When a job fires after its current firing; None once a one-off has fired."""
    if job.repeat is None:
        return None
    upcoming = following(job.next_at, job.repeat, tz, job.anchor_at)
    while upcoming <= now:  # skip what was missed while Derp was down
        upcoming = following(upcoming, job.repeat, tz, job.anchor_at)
    return upcoming


def keep_wall_time(job: Job, old: tzinfo, new: tzinfo, now: datetime) -> None:
    """Re-anchor a repeating job to the same local wall time and day in a new zone (one-offs stay put)."""
    if job.repeat is None:
        return
    first = job.anchor_at.astimezone(old)
    job.anchor_at = datetime.combine(first.date(), first.time(), new).astimezone(UTC)
    upcoming = datetime.combine(job.next_at.astimezone(old).date(), first.time(), new).astimezone(UTC)
    while upcoming <= now:
        upcoming = following(upcoming, job.repeat, new, job.anchor_at)
    job.next_at = upcoming


async def rezone(session: AsyncSession, chat: Chat, zone: str, now: datetime) -> None:
    """Move a (locked) chat to ``zone``; its repeating jobs keep their wall time unless it had no zone."""
    old = chat_time(chat) if chat.timezone else None
    chat.timezone = zone
    if old is None:  # placed as instants: nothing was ever local
        return
    repeating = await session.scalars(
        select(Job)
        .where(Job.chat_id == chat.id, Job.state.in_(LIVE), Job.repeat.is_not(None))
        .with_for_update()
    )
    for job in repeating:
        keep_wall_time(job, old, ZoneInfo(zone), now)


def resume(job: Job, now: datetime, tz: tzinfo) -> bool:
    """Reactivate a paused job, moving a missed time on; False when a one-off's time has passed."""
    if job.next_at <= now:
        upcoming = next_after(job, now, tz)
        if upcoming is None:
            return False
        job.next_at = upcoming
    job.state, job.misses = "active", 0
    return True


async def live_jobs(session: AsyncSession, chat_id: int) -> list[tuple[Job, str | None]]:
    """The chat's active and paused jobs, soonest first, with their creator's first name."""
    rows = await session.execute(
        select(Job, User.first_name)
        .outerjoin(User, User.id == Job.creator_id)
        .where(Job.chat_id == chat_id, Job.state.in_(LIVE))
        .order_by(Job.next_at)
    )
    return [(job, name) for job, name in rows]


def may_touch(
    job: Job, user_id: int | None, admin: bool, action: JobAction | Change | Snooze | None = None
) -> bool:
    """Its creator or an admin may pause, resume, change, snooze or cancel a job, except that a change to
    what a prompt costs its creator (``_costs_creator``) is the creator's alone."""
    if job.creator_id == user_id:
        return True
    return admin and not (isinstance(action, Change) and _costs_creator(job, action))


async def act_on_job(
    session: AsyncSession,
    chat: Chat,
    job_id: int,
    user_id: int,
    admin: bool,
    action: JobAction | Change | Snooze,
) -> Job | Refusal:
    """Pause, resume, cancel, change or snooze one of this chat's jobs; returns the job, or why not.

    A snoozed repeating job returns its extra firing. Resuming an active job leaves it be; cancelling a
    series cancels its extra firing too. ``admin`` is always true in a private chat.
    """
    job = await session.get(Job, job_id, with_for_update=True)
    if job is None or job.chat_id != chat.id or job.state == "cancelled":
        return "gone"
    if not may_touch(job, user_id, admin, action):
        return "creators" if admin else "not_yours"
    if isinstance(action, Snooze):
        if job.runs != action.run:
            return "fired"
        if job.repeat is not None:
            return await _extra(session, job, action.until)
        action = Change(at=action.until)
    if job.state == "done" and not isinstance(action, Change):
        return "gone"
    now, tz = datetime.now(UTC), chat_time(chat)
    match action:
        case "pause":
            job.state = "paused"
            await session.execute(update(Job).where(_extras(job, "active")).values(state="paused"))
        case "resume" if job.state == "paused":
            if not resume(job, now, tz):
                return "passed"
            for extra in await session.scalars(select(Job).where(_extras(job, "paused")).with_for_update()):
                if not resume(extra, now, tz):  # its moment passed while paused
                    extra.state = "cancelled"
        case "cancel":
            job.state = "cancelled"
            await session.execute(update(Job).where(_extras(job, *LIVE)).values(state="cancelled"))
        case Change():
            return await _change(session, job, action, admin, now, tz)
        case _:
            pass
    return job


def _extras(job: Job, *states: str) -> ColumnElement[bool]:
    """A repeating job's snoozed extra firing in one of ``states``: it pauses, resumes and goes with its series."""
    return (Job.snooze_of == job.id) & Job.state.in_(states)


def _repeat(job: Job, change: Change) -> str | None:
    """The job's repeat rule after ``change``."""
    return job.repeat if change.repeat is None else None if change.repeat == "once" else change.repeat


def _costs_creator(job: Job, change: Change) -> bool:
    """The change makes, rewords or re-repeats a prompt, or turns one into a reminder: a prompt fires on its
    creator's Derpcoins, so what it is and how often it runs is theirs to decide."""
    if "prompt" not in (job.kind, change.kind):
        return False
    return (
        change.kind not in (None, job.kind)
        or change.text not in (None, job.text)
        or _repeat(job, change) != job.repeat
    )


async def _change(
    session: AsyncSession, job: Job, change: Change, admin: bool, now: datetime, tz: tzinfo
) -> Job | Refusal:
    repeat = _repeat(job, change)
    if repeat and not admin:
        return "admins"
    if job.state == "done":  # a one-off that fired: only a new time brings it back
        if change.at is None:
            return "passed"
        if not await has_room(session, job.chat_id, job.creator_id):
            return "full"
    if change.at is not None:
        job.next_at = job.anchor_at = change.at
        job.state, job.misses = "active", 0
    elif repeat is not None and repeat != job.repeat:
        job.next_at = first_after(now, repeat, tz, job.anchor_at)
    job.repeat = repeat
    job.text = change.text or job.text
    job.kind = change.kind or job.kind
    return job


async def _extra(session: AsyncSession, job: Job, until: datetime) -> Job | Refusal:
    """Move a repeating job's extra firing to ``until``, adding it if it has none pending."""
    extra = await session.scalar(
        select(Job).where(Job.snooze_of == job.id, Job.state == "active").with_for_update()
    )
    if extra is None:
        if not await has_room(session, job.chat_id, job.creator_id):
            return "full"
        extra = Job(
            chat_id=job.chat_id,
            thread_id=job.thread_id,
            creator_id=job.creator_id,
            kind=job.kind,
            text=job.text,
            snooze_of=job.id,
            state="active",
        )
        session.add(extra)
    extra.next_at = extra.anchor_at = until
    await session.flush()
    return extra


async def pause_jobs(session: AsyncSession, chat_id: int, creator_id: int | None = None) -> None:
    """Pause a chat's active jobs (Derp was removed), or one member's (they left)."""
    where = [Job.chat_id == chat_id, Job.state == "active"]
    if creator_id is not None:
        where.append(Job.creator_id == creator_id)
    await session.execute(update(Job).where(*where).values(state="paused"))


def _iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.strip().replace(" ", "T", 1))
    except ValueError:
        return None


def parse_local(text: str, tz: tzinfo) -> datetime | None:
    """An ISO date-time written in the chat's time zone (an explicit offset wins)."""
    moment = _iso(text)
    if moment is None:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=tz)).astimezone(UTC)


def has_offset(text: str) -> bool:
    """The ISO time names its own offset (``+03:00``, ``Z``), so it needs no chat time zone."""
    moment = _iso(text)
    return moment is not None and moment.tzinfo is not None


def local_time(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%a %d %b %H:%M")


def show_time(moment: datetime, tz: tzinfo) -> str:
    """Markdown that Telegram renders in each viewer's own time zone, with a chat-local fallback."""
    return f"![{local_time(moment, tz)}](tg://time?unix={int(moment.timestamp())}&format=wDT)"


async def has_room(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """Whether one more live job fits the chat's and this person's limits."""
    live = (Job.chat_id == chat_id) & Job.state.in_(LIVE)
    total = await session.scalar(select(func.count()).where(live)) or 0
    mine = await session.scalar(select(func.count()).where(live, Job.creator_id == user_id)) or 0
    return total < CHAT_LIMIT and mine < PERSON_LIMIT
