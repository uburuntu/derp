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

Creators pause, resume and cancel their own jobs, admins any (``act_on_job``,
behind the agent's tools, the /settings jobs page and the Resume button).
"""

import calendar
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from sqlalchemy import func, select, update
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
type Refusal = Literal["gone", "not_yours", "passed"]


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


def may_touch(job: Job, user_id: int | None, admin: bool) -> bool:
    """Its creator or an admin may pause, resume or cancel a job."""
    return admin or job.creator_id == user_id


async def act_on_job(
    session: AsyncSession, chat: Chat, job_id: int, user_id: int, admin: bool, action: JobAction
) -> Job | Refusal:
    """Pause, resume or cancel one of this chat's jobs; returns the job, or why it was refused.

    Resuming an active job leaves it be.
    """
    job = await session.get(Job, job_id, with_for_update=True)
    if job is None or job.chat_id != chat.id or job.state not in LIVE:
        return "gone"
    if not may_touch(job, user_id, admin):
        return "not_yours"
    match action:
        case "pause":
            job.state = "paused"
        case "resume" if job.state == "paused":
            if not resume(job, datetime.now(UTC), chat_time(chat)):
                return "passed"
        case "cancel":
            job.state = "cancelled"
    return job


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


async def active_counts(session: AsyncSession, chat_id: int, user_id: int) -> tuple[int, int]:
    live = (Job.chat_id == chat_id) & Job.state.in_(LIVE)
    total = await session.scalar(select(func.count()).where(live)) or 0
    mine = await session.scalar(select(func.count()).where(live, Job.creator_id == user_id)) or 0
    return int(total), int(mine)
