"""Privacy actions: /forget, the context switch's purge, and the daily retention job.

``/forget`` as a reply to one's own message forgets that message; alone it
offers to forget everything the person said in this chat (reactions, votes,
membership history and personal notes too). Stored run transcripts that may
quote what was forgotten are dropped as well; usage and charges stay.
"""

from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import Message
from sqlalchemy import delete, func, select

from derp.agent.models import Approval
from derp.agent.runs import expire_run_messages, scrub_runs
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.history.models import Message as StoredMessage
from derp.history.retention import forget_member, forget_message, purge_ambient, purge_expired
from derp.locales import resolve, t
from derp.memory.models import Note
from derp.scheduler.models import Job
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown

FINISHED_JOB_DAYS = 30


class ForgetAll(CallbackData, prefix="forget"):
    user_id: int


async def forget_one(db: Database, chat_id: int, message_id: int) -> None:
    now = datetime.now(UTC)
    async with db.transaction() as session:
        stored = await session.get(StoredMessage, (chat_id, message_id))
        if stored is None:
            return
        await forget_message(session, chat_id, message_id, now)
        await scrub_runs(session, chat_id, since=stored.date)


async def forget_everything(db: Database, chat_id: int, user_id: int) -> int:
    now = datetime.now(UTC)
    async with db.transaction() as session:
        first = await session.scalar(
            select(func.min(StoredMessage.date)).where(
                StoredMessage.chat_id == chat_id, StoredMessage.user_id == user_id
            )
        )
        count = await forget_member(session, chat_id, user_id, now)
        await session.execute(delete(Note).where(Note.chat_id == chat_id, Note.user_id == user_id))
        await session.execute(delete(Job).where(Job.chat_id == chat_id, Job.creator_id == user_id))
        await session.execute(
            delete(Approval).where(Approval.chat_id == chat_id, Approval.user_id == user_id)
        )
        if first is not None:
            await scrub_runs(session, chat_id, since=first)
    return count


async def switch_context(db: Database, chat_id: int, *, on: bool) -> int:
    """Turn ambient capture on or off; off deletes what ambient capture stored. Returns messages purged."""
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id, with_for_update=True)
        if chat is None:
            return 0
        chat.ambient = on
        if on:
            return 0
        purged = await purge_ambient(session, chat_id)
        await scrub_runs(session, chat_id)
    return purged


async def enforce_retention(db: Database, settings: Settings) -> int:
    now = datetime.now(UTC)
    async with db.transaction() as session:
        deleted = await purge_expired(session, settings.retention, now)
        await expire_run_messages(session, settings.retention, now)
        finished = Job.state.in_(("done", "cancelled")) & (
            Job.created_at < now - timedelta(days=FINISHED_JOB_DAYS)
        )
        await session.execute(delete(Job).where(finished))
    return deleted


async def on_forget(message: Message, db: Database, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    replied = message.reply_to_message
    if replied is not None:
        if replied.from_user is None or replied.from_user.id != message.from_user.id:
            await send_markdown(bot, target, t(locale, "privacy.only_own"))
            return
        await forget_one(db, message.chat.id, replied.message_id)
        await send_markdown(bot, target, t(locale, "privacy.forgot_one"))
        return
    async with db.session() as session:
        count = await session.scalar(
            select(func.count()).where(
                StoredMessage.chat_id == message.chat.id,
                StoredMessage.user_id == message.from_user.id,
                StoredMessage.deleted_at.is_(None),
            )
        )
    confirm = button(
        t(locale, "privacy.confirm_button"), ForgetAll(user_id=message.from_user.id), style="danger"
    )
    await send_markdown(
        bot, target, t(locale, "privacy.confirm", count=count or 0), reply_markup=markup(confirm)
    )


async def on_forget_all(press: Press, data: ForgetAll, db: Database) -> None:
    """Only the person who asked; the prompt then says what was forgotten, without its button."""
    if data.user_id != press.user_id:
        return await press.deny("privacy.not_yours")
    count = await forget_everything(db, press.chat_id, press.user_id)
    if (
        not count
    ):  # nothing stored (or a second tap after the first one forgot it all): keep the prompt's text
        await press.strip()
        return await press.toast("privacy.forgot_all", count=0)
    await press.edit(t(press.locale, "privacy.forgot_all", count=count))


def build_router() -> Router:
    router = Router(name="privacy")
    router.message.register(on_forget, Command("forget"))
    on_press(router, ForgetAll, F.user_id > 0)(on_forget_all)
    return router
