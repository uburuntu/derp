"""Feedback for the owners: /feedback, the agent's leave_feedback, and the owners' /feedbacks review.

People's words are stored as they wrote them and shown only to owners, in a
private chat, a page at a time; an owner marks items done. /forget removes a
person's feedback from that chat with the rest of what they said.
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.ops.models import FEEDBACK_KINDS, Feedback
from derp.ops.owner import is_blocked
from derp.telegram.sender import Target, rich, sanitize_untrusted, send_markdown

PAGE = 5
MAX_CHARS = 2000


class FeedbackAction(CallbackData, prefix="fb"):
    action: str  # page | done
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
    buttons: list[InlineKeyboardButton] = []
    for item, name, username, title in rows:
        who = one_line(name or "someone") + (f" @{username}" if username else "")
        where = "private" if item.chat_id == item.user_id else one_line(title or str(item.chat_id))
        lines.append(
            f"\n#{item.id} · {item.kind} · {who} · {where} · {item.created_at:%Y-%m-%d}\n> {sanitize_untrusted(one_line(item.text, 600))}"
        )
        buttons.append(
            InlineKeyboardButton(
                text=f"✓ #{item.id}", callback_data=FeedbackAction(action="done", value=item.id).pack()
            )
        )
    rows_markup = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="‹ Newer",
                callback_data=FeedbackAction(action="page", value=max(0, offset - PAGE)).pack(),
            )
        )
    if offset + PAGE < total:
        nav.append(
            InlineKeyboardButton(
                text="Older ›", callback_data=FeedbackAction(action="page", value=offset + PAGE).pack()
            )
        )
    if nav:
        rows_markup.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows_markup)


async def on_feedbacks(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if (
        message.from_user is None
        or message.from_user.id not in settings.owner_ids
        or message.chat.type != "private"
    ):
        return
    text, markup = await _page(db, 0)
    await send_markdown(bot, Target(message.chat.id), text, reply_markup=markup)


async def on_review(
    query: CallbackQuery, callback_data: FeedbackAction, db: Database, settings: Settings, bot: Bot
) -> None:
    message = query.message
    if query.from_user.id not in settings.owner_ids or not isinstance(message, Message):
        await query.answer()
        return
    offset = 0
    if callback_data.action == "done":
        async with db.transaction() as session:
            await session.execute(
                update(Feedback).where(Feedback.id == callback_data.value).values(state="done")
            )
    else:
        offset = max(0, callback_data.value)
    await query.answer()
    text, markup = await _page(db, offset)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=message.message_id,
        rich_message=rich(text),
        reply_markup=markup,
    )


def build_router() -> Router:
    router = Router(name="feedback")
    router.message.register(on_feedback, Command("feedback"))
    router.message.register(on_feedbacks, Command("feedbacks"))
    router.callback_query.register(on_review, FeedbackAction.filter(F.action.in_({"page", "done"})))
    return router
