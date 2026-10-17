"""Feedback for the owners: /feedback, the agent's leave_feedback, and the owners' /feedbacks review.

People's words are stored as they wrote them and shown only to owners, in a
private chat, a page at a time; an owner marks items done, or done with a
thank-you DM to the author (at most once: only the press that closes the item
sends it). /forget removes a person's feedback from that chat with the rest of
what they said.
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.ops.models import FEEDBACK_KINDS, Feedback
from derp.ops.owner import from_owner, is_blocked
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, sanitize_untrusted, send_markdown, send_privately
from derp.telegram.text import literal

PAGE = 5
MAX_CHARS = 2000


class FeedbackAction(CallbackData, prefix="fb"):
    action: str  # page | done | thank (done, and the author gets a thank-you)
    value: int = 0  # page offset, or the feedback id


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


async def _page(db: Database, offset: int) -> tuple[str, InlineKeyboardMarkup | None]:
    async with db.session() as session:
        total = await session.scalar(select(func.count()).where(Feedback.state == "open")) or 0
        rows = (
            await session.execute(
                select(Feedback, User.first_name, User.username, Chat.title)
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
    for item, name, username, title in rows:
        who = literal(one_line(name or "someone")) + (f" @{username}" if username else "")
        where = "private" if item.chat_id == item.user_id else literal(one_line(title or str(item.chat_id)))
        lines.append(
            f"\n#{item.id} · {item.kind} · {who} · {where} · {item.created_at:%Y-%m-%d}\n> {sanitize_untrusted(one_line(item.text, 600))}"
        )
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
    on_press(router, FeedbackAction, F.action.in_({"page", "done", "thank"}))(on_review)
    return router
