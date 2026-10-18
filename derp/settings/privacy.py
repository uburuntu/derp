"""Privacy actions: /forget, the context switch's purge, and the daily retention job.

``/forget`` as a reply to one's own message forgets that message; alone it
offers to forget everything the person said in this chat (reactions, votes,
membership history and personal notes too) and, to admins (anyone in private),
everything Derp knows about the chat: every message, notes, summaries and run
transcripts, asked twice and then announced on the prompt itself. Rules,
scheduled jobs, games, settings and money stay. Stored run transcripts that may
quote what was forgotten are dropped as well; usage and charges stay.
"""

from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select

from derp.agent.models import Approval
from derp.agent.runs import expire_run_messages, scrub_runs
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.history.models import Message as StoredMessage
from derp.history.render import one_line
from derp.history.retention import forget_chat, forget_member, forget_message, purge_ambient, purge_expired
from derp.locales import resolve, t
from derp.memory.models import Note
from derp.memory.notes import is_admin
from derp.scheduler.models import Job
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal

FINISHED_JOB_DAYS = 30


class ForgetAll(CallbackData, prefix="forget"):
    user_id: int


class ForgetChat(CallbackData, prefix="forgetchat"):
    user_id: int  # the admin who asked; only they may go on
    step: str  # ask (show what goes) | go (forget) | back (the first prompt)


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


async def forget_whole_chat(db: Database, chat_id: int) -> tuple[int, int]:
    """Everything Derp knows about the chat; returns the messages and notes forgotten."""
    now = datetime.now(UTC)
    async with db.transaction() as session:
        count = await forget_chat(session, chat_id, now)
        notes = await session.execute(delete(Note).where(Note.chat_id == chat_id))
        await session.execute(delete(Approval).where(Approval.chat_id == chat_id))
        await scrub_runs(session, chat_id)
    return count, int(getattr(notes, "rowcount", 0) or 0)


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


async def _prompt(
    db: Database, chat: Chat, user_id: int, admin: bool, locale: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The /forget prompt: everything the person said, and the whole chat for admins."""
    async with db.session() as session:
        count = await session.scalar(
            select(func.count()).where(
                StoredMessage.chat_id == chat.id,
                StoredMessage.user_id == user_id,
                StoredMessage.deleted_at.is_(None),
            )
        )
    mine = button(t(locale, "privacy.confirm_button"), ForgetAll(user_id=user_id), style="danger")
    whole = (
        button(t(locale, "privacy.chat_button"), ForgetChat(user_id=user_id, step="ask")) if admin else None
    )
    return t(locale, "privacy.confirm", count=count or 0), markup(mine, whole)


async def on_forget(message: Message, db: Database, bot: Bot, chat_row: Chat) -> None:
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
    admin = await is_admin(bot, chat_row, message.from_user.id)
    text, keyboard = await _prompt(db, chat_row, message.from_user.id, admin, locale)
    await send_markdown(bot, target, text, reply_markup=keyboard)


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


async def on_forget_chat(press: Press, data: ForgetChat, db: Database, bot: Bot) -> None:
    """The admin who asked, still an admin: ask with the counts, then forget and tell the chat."""
    if data.user_id != press.user_id:
        return await press.deny("privacy.not_yours")
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None:
        return None
    if not await is_admin(bot, chat, press.user_id):
        return await press.deny("privacy.admins_only")
    locale = press.locale
    if data.step == "back":
        return await press.edit(*await _prompt(db, chat, press.user_id, True, locale))
    if data.step == "ask":
        async with db.session() as session:
            messages = await session.scalar(
                select(func.count()).where(
                    StoredMessage.chat_id == chat.id, StoredMessage.deleted_at.is_(None)
                )
            )
            notes = await session.scalar(select(func.count()).where(Note.chat_id == chat.id))
        go = button(
            t(locale, "privacy.chat_confirm_button"),
            ForgetChat(user_id=press.user_id, step="go"),
            style="danger",
        )
        back = button(t(locale, "settings.back"), ForgetChat(user_id=press.user_id, step="back"))
        text = t(locale, "privacy.chat_confirm", messages=messages or 0, notes=notes or 0)
        return await press.edit(text, markup([go, back]))
    messages, notes = await forget_whole_chat(db, chat.id)
    if not (messages or notes):  # a second tap after the first one forgot it all: the notice stands
        await press.strip()
        return await press.toast("privacy.chat_forgotten_toast")
    name = literal(one_line(press.query.from_user.full_name))
    key = "privacy.chat_forgotten_private" if chat.type == "private" else "privacy.chat_forgotten"
    await press.edit(t(locale, key, name=name, messages=messages, notes=notes))
    return None


def build_router() -> Router:
    router = Router(name="privacy")
    router.message.register(on_forget, Command("forget"))
    on_press(router, ForgetAll, F.user_id > 0)(on_forget_all)
    on_press(router, ForgetChat, F.step.in_({"ask", "go", "back"}))(on_forget_chat)
    return router
