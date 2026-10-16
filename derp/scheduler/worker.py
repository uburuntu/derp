"""The single job worker: claim due jobs, move them on, then fire them.

A job is claimed and rescheduled (or finished) in one transaction before it
fires, so a crash mid-send never fires it twice. Prompt jobs run as an agent
turn with the creator as invoker, funded at fire time like any message; an
unfunded firing posts a short note, and ``MISS_LIMIT`` of them in a row pause
the job. A chat that refuses Derp's messages pauses its jobs.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat as TgChat
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser
from sqlalchemy import select, update

from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import chat_time
from derp.locales import resolve, t
from derp.ops.owner import is_blocked
from derp.scheduler.jobs import MISS_LIMIT, next_after
from derp.scheduler.models import Job
from derp.telegram.sender import Target, send_markdown

if TYPE_CHECKING:
    from derp.agent.runtime import Runtime

log = logging.getLogger("derp.scheduler")
BATCH = 10
FIRING_LIMIT = 8  # jobs firing at once; the rest wait for the next tick


async def claim_due(db: Database, now: datetime, limit: int = BATCH) -> list[tuple[Job, Chat]]:
    async with db.transaction() as session:
        due = list(
            await session.scalars(
                select(Job)
                .where(Job.state == "active", Job.next_at <= now)
                .order_by(Job.next_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        claimed: list[tuple[Job, Chat]] = []
        for job in due:
            chat = await session.get(Chat, job.chat_id)
            upcoming = next_after(job, now, chat_time(chat))
            job.last_fired_at, job.runs = now, job.runs + 1
            if upcoming is None:
                job.state = "done"
            else:
                job.next_at = upcoming
            if chat is not None:
                claimed.append((job, chat))
    return claimed


def as_message(job: Job, chat: Chat, user: User | None, now: datetime) -> TgMessage:
    """The scheduled prompt as an invocation from its creator (never stored: id 0)."""
    tg_chat = TgChat(id=chat.id, type=chat.type, title=chat.title, is_forum=chat.is_forum or None)
    sender = TgUser(
        id=job.creator_id,
        is_bot=False,
        first_name=(user.first_name if user else None) or "someone",
        last_name=user.last_name if user else None,
        language_code=user.language_code if user else None,
    )
    return TgMessage(
        message_id=0,
        date=now,
        chat=tg_chat,
        from_user=sender,
        text=f"⏰ (scheduled task) {job.text}",
        message_thread_id=job.thread_id,
        is_topic_message=job.thread_id is not None or None,
    )


async def _set(db: Database, job_id: int, **values: object) -> None:
    async with db.transaction() as session:
        await session.execute(update(Job).where(Job.id == job_id).values(**values))


async def fire(db: Database, bot: Bot, runtime: Runtime, job: Job, chat: Chat, now: datetime) -> None:
    target = Target(chat.id, job.thread_id)
    async with db.session() as session:
        user = await session.get(User, job.creator_id)
        blocked = await is_blocked(session, chat.id, job.creator_id)
    if blocked:
        log.info("job %s paused: an owner blocked its chat or creator", job.id)
        await _set(db, job.id, state="paused")
        return
    locale = chat.locale or resolve(user.language_code if user else None)
    try:
        if job.kind == "message":
            name = (user.first_name if user else None) or "someone"
            await send_markdown(
                bot, target, t(locale, "scheduler.reminder", name=name, text=job.text), trusted=False
            )
            return
        outcome = await runtime.answer(as_message(job, chat, user, now), chat, trigger="job")
        if outcome is not None and outcome.refused:
            log.info("job %s paused: the chat refused the answer", job.id)
            await _set(db, job.id, state="paused")
            return
        if outcome is None or outcome.status != "unfunded":
            await _set(db, job.id, misses=0)
            return
        misses = job.misses + 1
        paused = misses >= MISS_LIMIT
        await _set(db, job.id, misses=misses, **({"state": "paused"} if paused else {}))
        await send_markdown(
            bot, target, t(locale, "scheduler.paused" if paused else "scheduler.skipped", id=job.id)
        )
    except (TelegramForbiddenError, TelegramBadRequest) as error:  # removed from the chat, topic gone…
        log.info("job %s paused: %s", job.id, type(error).__name__)
        await _set(db, job.id, state="paused")


async def _fire_logged(db: Database, bot: Bot, runtime: Runtime, job: Job, chat: Chat, now: datetime) -> None:
    try:
        await fire(db, bot, runtime, job, chat, now)
    except Exception as error:  # noqa: BLE001 - one job must not take the others down
        log.error("job %s failed: %s", job.id, type(error).__name__)


async def run_due(db: Database, bot: Bot, runtime: Runtime) -> int:
    """Claim what is due and fire it all, returning when every firing finished."""
    now = datetime.now(UTC)
    claimed = await claim_due(db, now)
    await asyncio.gather(*(_fire_logged(db, bot, runtime, job, chat, now) for job, chat in claimed))
    return len(claimed)


class Firings:
    """Fires due jobs in the background, so one slow scheduled prompt never delays other chats' reminders."""

    def __init__(self, limit: int = FIRING_LIMIT) -> None:
        self.limit = limit
        self.tasks: set[asyncio.Task[None]] = set()

    async def tick(self, db: Database, bot: Bot, runtime: Runtime) -> int:
        room = self.limit - len(self.tasks)
        if room <= 0:
            return 0
        now = datetime.now(UTC)
        claimed = await claim_due(db, now, min(BATCH, room))
        for job, chat in claimed:
            task = asyncio.create_task(_fire_logged(db, bot, runtime, job, chat, now))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return len(claimed)
