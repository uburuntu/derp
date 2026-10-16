"""Run / Cancel before expensive actions: the approval prompt and its buttons.

An action asks first when it costs more than ``approval_credits`` or more than
``approval_balance_fraction`` of what the payer has. The prompt shows only the
price (never a balance, since groups see it). Only the requester can decide; a
decision is final, an undecided prompt expires after ``approval_minutes``.
Running funds the action afresh (the asking run is long settled); denial and
expiry cost nothing.
"""

import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, Message
from sqlalchemy import delete

from derp.agent.models import Approval
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.locales import resolve, t
from derp.telegram.sender import Target, send_markdown

if TYPE_CHECKING:  # the runtime imports the tools that create approvals
    from derp.agent.runtime import Runtime


class ApprovalAction(CallbackData, prefix="approve"):
    id: int
    run: bool


def needs_approval(settings: Settings, price: int, payer_balance: int) -> bool:
    return price > settings.approval_credits or price > settings.approval_balance_fraction * payer_balance


def keyboard(approval_id: int, locale: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(locale, "approvals.run"),
                    callback_data=ApprovalAction(id=approval_id, run=True).pack(),
                    style="success",
                ),
                InlineKeyboardButton(
                    text=t(locale, "approvals.cancel"),
                    callback_data=ApprovalAction(id=approval_id, run=False).pack(),
                    style="danger",
                ),
            ]
        ]
    )


async def ask(
    db: Database,
    bot: Bot,
    settings: Settings,
    target: Target,
    *,
    kind: str,
    args: dict[str, object],
    price: int,
    user_id: int,
    run_id: int,
    locale: str,
) -> Message:
    """Store the pending action and post its Run / Cancel prompt."""
    async with db.transaction() as session:
        approval = Approval(
            run_id=run_id,
            chat_id=target.chat_id,
            thread_id=target.thread_id,
            user_id=user_id,
            reply_to=target.reply_to,
            kind=kind,
            args=args,
            credits=price,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.approval_minutes),
        )
        session.add(approval)
        await session.flush()
        approval_id = approval.id
    text = t(locale, f"approvals.{kind}", credits=f"{price:,}")
    sent = (await send_markdown(bot, target, text, reply_markup=keyboard(approval_id, locale)))[-1]
    async with db.transaction() as session:
        if (row := await session.get(Approval, approval_id)) is not None:
            row.prompt_message_id = sent.message_id
    return sent


async def _close_prompt(bot: Bot, message: Message, text: str) -> None:
    with contextlib.suppress(TelegramAPIError):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message.message_id,
            rich_message=InputRichMessage(markdown=text),
        )


async def on_decision(
    query: CallbackQuery, callback_data: ApprovalAction, db: Database, bot: Bot, runtime: Runtime
) -> None:
    locale = resolve(query.from_user.language_code)
    message = query.message
    now = datetime.now(UTC)
    async with db.transaction() as session:
        approval = await session.get(Approval, callback_data.id, with_for_update=True)
        if approval is None or approval.user_id != query.from_user.id:
            await query.answer(t(locale, "approvals.not_yours"), show_alert=True)
            return
        if approval.state == "pending" and approval.expires_at <= now:
            approval.state = "expired"
        if approval.state != "pending":
            await query.answer(t(locale, f"approvals.already_{approval.state}"), show_alert=True)
            return
        approval.state, approval.decided_at = ("approved" if callback_data.run else "denied"), now
        chat = await session.get(Chat, approval.chat_id)
    await query.answer()
    if isinstance(message, Message):
        await _close_prompt(
            bot, message, t(locale, "approvals.making" if callback_data.run else "approvals.cancelled")
        )
    if callback_data.run and chat is not None:
        await runtime.run_approved(approval, chat, locale)


async def drop_old(db: Database, before: datetime) -> None:
    """Approvals hold the requested prompt text; they are not kept past a day."""
    async with db.transaction() as session:
        await session.execute(delete(Approval).where(Approval.created_at < before))


def build_router() -> Router:
    router = Router(name="approvals")
    router.callback_query.register(on_decision, ApprovalAction.filter(F.id > 0))
    return router
