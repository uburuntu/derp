"""Job rules: who may schedule what, when it fires next, and how times are shown.

Repeats keep the first occurrence's wall-clock time in the chat's time zone (so
09:00 stays 09:00 across daylight-saving changes, and a time skipped by a
spring-forward gap returns the next day) and its day of month (a job on the
31st fires on the last day of shorter months, then on the 31st again).
Firings missed while Derp was down are skipped, not replayed: a job fires at
most once per claim.
"""

import calendar
from datetime import UTC, date, datetime, timedelta, tzinfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.scheduler.models import Job

MAX_AHEAD = timedelta(days=366)
MIN_AHEAD = timedelta(seconds=30)
CHAT_LIMIT = 25  # active jobs per chat
PERSON_LIMIT = 10  # active jobs per person per chat
MISS_LIMIT = 3  # unfunded firings in a row before a job pauses


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


async def pause_jobs(session: AsyncSession, chat_id: int, creator_id: int | None = None) -> None:
    """Pause a chat's active jobs (Derp was removed), or one member's (they left)."""
    where = [Job.chat_id == chat_id, Job.state == "active"]
    if creator_id is not None:
        where.append(Job.creator_id == creator_id)
    await session.execute(update(Job).where(*where).values(state="paused"))


def parse_local(text: str, tz: tzinfo) -> datetime | None:
    """An ISO date-time written in the chat's time zone (an explicit offset wins)."""
    try:
        moment = datetime.fromisoformat(text.strip().replace(" ", "T", 1))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=tz)).astimezone(UTC)


def show_time(moment: datetime, tz: tzinfo) -> str:
    """Markdown that Telegram renders in each viewer's own time zone, with a chat-local fallback."""
    fallback = moment.astimezone(tz).strftime("%a %d %b %H:%M")
    return f"![{fallback}](tg://time?unix={int(moment.timestamp())}&format=wDT)"


async def active_counts(session: AsyncSession, chat_id: int, user_id: int) -> tuple[int, int]:
    live = (Job.chat_id == chat_id) & Job.state.in_(("active", "paused"))
    total = await session.scalar(select(func.count()).where(live)) or 0
    mine = await session.scalar(select(func.count()).where(live, Job.creator_id == user_id)) or 0
    return int(total), int(mine)
