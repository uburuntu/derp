"""Feedback for the owners: /feedback, the agent's leave_feedback, /info's Report a problem, and the
owners' /feedbacks review.

People's words are stored as they wrote them and shown only to owners, in a
private chat, a page at a time; an owner marks items done, or done with a
thank-you DM to the author (at most once: only the press that closes the item
sends it). /forget removes a person's feedback from that chat with the rest of
what they said.

A report on one answer starts from the /info receipt's button (ephemeral in
groups; buttons there work like anywhere else): a category files it at once,
with the answer's run and message, one report per person per answer (a second
press changes its category). For ``DETAILS_WINDOW`` the author's next text
message in the chat where they pressed (``details_chat_id``; not a command, and
in groups not addressed to Derp) becomes its text, unless they skip. A person
has one window at a time: opening one closes their others, and a report that
already has its text keeps it when recategorized. ``details_for`` is looked up
by ``CaptureMiddleware`` in its per-message transaction, which hands the
handlers ``report_id``.
"""

import contextlib
from datetime import UTC, datetime, timedelta
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.invocation import Trigger, starts_with_command
from derp.agent.models import AgentRun
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.ops.models import FEEDBACK_KINDS, REPORT_KINDS, Feedback
from derp.ops.owner import from_owner, is_blocked
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, sanitize_untrusted, send_markdown, send_privately
from derp.telegram.text import literal

PAGE = 5
MAX_CHARS = 2000
DETAILS_WINDOW = timedelta(minutes=5)


class FeedbackAction(CallbackData, prefix="fb"):
    action: str  # page | done | thank (done, and the author gets a thank-you)
    value: int = 0  # page offset, or the feedback id


class ReportAction(CallbackData, prefix="report"):
    step: str  # open (show the categories) | kind (file it) | skip (no text)
    run: int
    message: int = 0  # the reported answer
    kind: str = ""


async def file(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    text: str,
    kind: str = "other",
    message_id: int | None = None,
    run_id: int | None = None,
) -> int:
    row = Feedback(
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id or None,
        run_id=run_id,
        kind=kind if kind in FEEDBACK_KINDS else "other",
        text=text.strip()[:MAX_CHARS],
    )
    session.add(row)
    await session.flush()
    return row.id


async def on_feedback(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    text = (command.args or "").strip()
    target = Target.reply_to_message(message)
    if not text:
        await send_markdown(bot, target, t(locale, "feedback.how"))
        return
    async with db.transaction() as session:
        if await is_blocked(session, message.chat.id, message.from_user.id):
            return  # blocked: nothing reaches the owners
        await file(
            session,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            text=text,
            message_id=message.message_id,
        )
    await send_markdown(bot, target, t(locale, "feedback.thanks"))


def report_button(locale: str, run_id: int, message_id: int) -> InlineKeyboardButton:
    """The /info receipt's way to report the answer."""
    return button(t(locale, "report.button"), ReportAction(step="open", run=run_id, message=message_id))


def _categories(locale: str, data: ReportAction) -> InlineKeyboardMarkup | None:
    def pick(kind: str) -> InlineKeyboardButton:
        return button(
            t(locale, f"report.{kind}"),
            ReportAction(step="kind", run=data.run, message=data.message, kind=kind),
        )

    return markup(*([pick(a), pick(b)] for a, b in zip(REPORT_KINDS[::2], REPORT_KINDS[1::2], strict=True)))


async def _file_report(db: Database, user_id: int, chat_id: int, data: ReportAction) -> bool | None:
    """File (or recategorize) the person's report on the run; None if the run is gone.

    True when the details window opened in ``chat_id`` (where the receipt was pressed), closing the person's
    other windows; False when the report already has its text (or the person is blocked: nothing reaches
    the owners, and nothing says so).
    """
    async with db.transaction() as session:
        run = await session.get(AgentRun, data.run, with_for_update=True)  # double taps file once
        if run is None:
            return None
        if await is_blocked(session, run.chat_id, user_id):
            return False
        report = await session.scalar(
            select(Feedback).where(
                Feedback.user_id == user_id,
                Feedback.run_id == run.id,
                Feedback.kind.in_(REPORT_KINDS),
                Feedback.state == "open",
            )
        )
        if report is None:
            report_id = await file(
                session, user_id=user_id, chat_id=run.chat_id, text="", kind=data.kind,
                message_id=data.message, run_id=run.id,
            )  # fmt: skip
            report = await session.get_one(Feedback, report_id)  # the row just added: no query
        report.kind = data.kind
        if report.text:
            return False
        await _close_windows(session, user_id)
        report.details_until, report.details_chat_id = datetime.now(UTC) + DETAILS_WINDOW, chat_id
    return True


async def _close_windows(session: AsyncSession, user_id: int, *more: ColumnElement[bool]) -> None:
    await session.execute(
        update(Feedback)
        .where(Feedback.user_id == user_id, Feedback.details_until.is_not(None), *more)
        .values(details_until=None, details_chat_id=None)
    )


async def on_report(press: Press, data: ReportAction, db: Database) -> None:
    locale = press.locale
    if data.step == "open":
        return await press.edit(t(locale, "report.ask"), _categories(locale, data))
    if data.step == "kind":
        opened = (
            await _file_report(db, press.user_id, press.chat_id, data) if data.kind in REPORT_KINDS else None
        )
        if opened is None:
            return await press.deny("buttons.stale")
        if not opened:
            return await press.edit(t(locale, "report.done"))
        skip = button(t(locale, "report.skip"), ReportAction(step="skip", run=data.run))
        private = press.message.chat.type == "private"
        return await press.edit(t(locale, "report.filed" if private else "report.filed_here"), markup(skip))
    async with db.transaction() as session:
        await _close_windows(session, press.user_id, Feedback.run_id == data.run)
    return await press.edit(t(locale, "report.done"))


async def details_for(session: AsyncSession, message: Message, trigger: Trigger | None) -> int | None:
    """The report whose text this message is: its author's next text message where they pressed, in the window.

    Commands are never details, nor in groups a message addressed to Derp (that is an ask).
    """
    sender = message.from_user
    if sender is None or not (message.text or message.caption) or starts_with_command(message):
        return None
    if message.chat.type != "private" and trigger is not None:
        return None
    return await session.scalar(
        select(Feedback.id).where(
            Feedback.details_chat_id == message.chat.id,
            Feedback.user_id == sender.id,
            Feedback.details_until > datetime.now(UTC),
        )
    )


async def giving_details(message: Message, report_id: int | None = None) -> bool:
    """The filter: ``CaptureMiddleware`` found this message to be a report's text."""
    return report_id is not None


async def on_details(message: Message, report_id: int, db: Database, bot: Bot) -> None:
    """Add the text to the report; private chats get a plain line, groups a quiet 👌 on the message."""
    text = message.text or message.caption or ""
    async with db.transaction() as session:
        added = await session.scalar(
            update(Feedback)
            .where(Feedback.id == report_id, Feedback.details_until.is_not(None))
            .values(text=text.strip()[:MAX_CHARS], details_until=None, details_chat_id=None)
            .returning(Feedback.id)
        )
    if added is None:
        return
    if message.chat.type == "private":
        locale = resolve(message.from_user.language_code if message.from_user else None)
        await send_markdown(bot, Target.reply_to_message(message), t(locale, "report.added"))
        return
    with contextlib.suppress(TelegramAPIError):
        await bot.set_message_reaction(
            chat_id=message.chat.id, message_id=message.message_id, reaction=[ReactionTypeEmoji(emoji="👌")]
        )


async def _page(db: Database, offset: int) -> tuple[str, InlineKeyboardMarkup | None]:
    async with db.session() as session:
        total = await session.scalar(select(func.count()).where(Feedback.state == "open")) or 0
        rows = (
            await session.execute(
                select(Feedback, User.first_name, User.username, Chat)
                .outerjoin(User, User.id == Feedback.user_id)
                .outerjoin(Chat, Chat.id == Feedback.chat_id)
                .where(Feedback.state == "open")
                .order_by(Feedback.id)
                .offset(offset)
                .limit(PAGE)
            )
        ).all()
    if not rows:
        return "No open feedback.", None
    lines = [f"**Open feedback: {total}** (showing {offset + 1}–{offset + len(rows)})"]
    items: list[list[InlineKeyboardButton]] = []
    for item, name, username, chat in rows:
        who = literal(one_line(name or "someone")) + (f" @{username}" if username else "")
        title = chat.title if chat is not None else None
        where = "private" if item.chat_id == item.user_id else literal(one_line(title or str(item.chat_id)))
        head = [f"#{item.id}", item.kind.replace("_", " "), who, where, f"{item.created_at:%Y-%m-%d}"]
        url = chat.message_link(item.message_id) if chat is not None and item.message_id else None
        if item.run_id is not None:
            head.append(f"[run {item.run_id}]({url})" if url else f"run {item.run_id}")
        elif url:
            head.append(f"[message]({url})")
        lines.append("\n" + " · ".join(head))
        if item.text:
            lines.append(f"> {sanitize_untrusted(one_line(item.text, 600))}")
        items.append(
            [
                button(f"✓ #{item.id}", FeedbackAction(action="done", value=item.id)),
                button(f"✓ & thank #{item.id}", FeedbackAction(action="thank", value=item.id)),
            ]
        )
    newer = FeedbackAction(action="page", value=max(0, offset - PAGE))
    older = FeedbackAction(action="page", value=offset + PAGE)
    nav = [
        button("‹ Newer", newer) if offset > 0 else None,
        button("Older ›", older) if offset + PAGE < total else None,
    ]
    return "\n".join(lines), markup(*items, nav)


async def on_feedbacks(message: Message, db: Database, bot: Bot) -> None:
    text, keyboard = await _page(db, 0)
    await send_markdown(bot, Target(message.chat.id), text, reply_markup=keyboard)


async def _close(db: Database, feedback_id: int) -> tuple[int, str | None] | None:
    """Mark an open item done: its author and their language, or None when it wasn't open any more."""
    async with db.transaction() as session:
        author = await session.scalar(
            update(Feedback)
            .where(Feedback.id == feedback_id, Feedback.state == "open")
            .values(state="done")
            .returning(Feedback.user_id)
        )
        if author is None:
            return None
        return author, await session.scalar(select(User.language_code).where(User.id == author))


async def on_review(press: Press, data: FeedbackAction, db: Database, settings: Settings, bot: Bot) -> None:
    if press.user_id not in settings.owner_ids:
        return  # the console doesn't announce itself: a silent answer
    offset = 0
    if data.action == "page":
        offset = max(0, data.value)
    elif (closed := await _close(db, data.value)) is None:
        await press.answer(f"#{data.value} was already done.", alert=True)
    elif data.action == "thank":
        author, language = closed
        if await send_privately(bot, author, t(resolve(language), "feedback.thanked")):
            await press.answer(f"#{data.value} done; thanked.")
        else:
            await press.answer(f"#{data.value} done, but the thank-you couldn't be delivered.", alert=True)
    text, keyboard = await _page(db, offset)
    await press.edit(text, keyboard)


def build_router() -> Router:
    router = Router(name="feedback")
    router.message.register(on_feedback, Command("feedback"))
    router.message.register(on_feedbacks, Command("feedbacks"), from_owner)
    router.message.register(on_details, giving_details)
    on_press(router, FeedbackAction, F.action.in_({"page", "done", "thank"}))(on_review)
    on_press(router, ReportAction, F.step.in_({"open", "kind", "skip"}))(on_report)
    return router
