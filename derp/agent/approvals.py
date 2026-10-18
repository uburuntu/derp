"""Make it / Cancel before expensive actions: the approval prompt, its buttons, and resuming the run.

An action asks first when it costs more than ``approval_credits`` or more than
``approval_balance_fraction`` of what the payer has. The prompt shows only the
price (never a balance, since groups see it), and the asking run pauses on the
call (Pydantic AI's deferred tools: the tool raises ``ApprovalRequired``). Only
the requester can decide; a decision is final, an undecided prompt expires after
``approval_minutes``: a press then, or the periodic sweep, turns the prompt into
the expired notice without buttons. Make it resumes the asking run from its
stored history (``resumption``): the call runs with exactly the job the prompt
priced and the model finishes what was asked (a caption, a sticker, a cutout),
funded afresh since the asking run is long settled. Denial and expiry charge
nothing for the action (the asking run paid its own turn when its prompt
landed). Once the run answered, its prompt is deleted, else it becomes the
failure note; an action the payer can no longer cover turns it into that note
with a buy button.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from pydantic_ai import DeferredToolResults, ToolDenied
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolReturnPart
from sqlalchemy import delete, select, update

from derp.agent.models import Approval
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.locales import resolve, t
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, rich, send_markdown

if TYPE_CHECKING:  # the runtime imports the tools that create approvals
    from derp.agent.runtime import Runtime

log = logging.getLogger("derp.approvals")
SWEEP = 50  # expired prompts closed per sweep; the rest wait for the next one


class ApprovalAction(CallbackData, prefix="approve"):
    id: int
    run: bool


def needs_approval(settings: Settings, price: int, payer_balance: int) -> bool:
    return price > settings.approval_credits or price > settings.approval_balance_fraction * payer_balance


def keyboard(approval_id: int, locale: str) -> InlineKeyboardMarkup | None:
    return markup(
        [
            button(t(locale, "approvals.run"), ApprovalAction(id=approval_id, run=True), style="success"),
            button(t(locale, "approvals.cancel"), ApprovalAction(id=approval_id, run=False), style="danger"),
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
    call: str,
    locale: str,
) -> Message:
    """Store the pending action (the paused ``call`` and its priced ``args``) and post its prompt."""
    async with db.transaction() as session:
        approval = Approval(
            run_id=run_id,
            chat_id=target.chat_id,
            thread_id=target.thread_id,
            user_id=user_id,
            reply_to=target.reply_to,
            kind=kind,
            args={"call": call, "job": args},
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


def resumption(approval: Approval, history: list[ModelMessage]) -> tuple[int, DeferredToolResults] | None:
    """Where the asking run paused on this approval's call (the index of that turn) and what resumes it.

    The approved call runs with the job the prompt priced (its ``tool_call_metadata``); another call
    paused in the same turn is declined here, since its own prompt resumes the run separately. None
    when the call isn't open at the end of ``history`` (the history was dropped or never paused).
    """
    call = approval.args.get("call")
    turns = [(i, m) for i, m in enumerate(history) if isinstance(m, ModelResponse)]
    if not turns:
        return None
    turn, paused = turns[-1]
    answered = {
        part.tool_call_id
        for message in history[turn + 1 :]
        for part in message.parts
        if isinstance(part, ToolReturnPart | RetryPromptPart)
    }
    waiting = [c.tool_call_id for c in paused.tool_calls if c.tool_call_id not in answered]
    if call not in waiting:
        return None
    resume = DeferredToolResults(metadata={str(call): approval.args["job"]})
    for other in waiting:
        resume.approvals[other] = (
            True if other == call else ToolDenied("Not approved: it has its own price prompt.")
        )
    return turn, resume


async def on_decision(press: Press, data: ApprovalAction, db: Database, runtime: Runtime) -> None:
    now = datetime.now(UTC)
    chat: Chat | None = None
    async with db.transaction() as session:
        approval = await session.get(Approval, data.id, with_for_update=True)
        if approval is not None and approval.state == "pending" and approval.expires_at <= now:
            approval.state = "expired"
        state = approval.state if approval is not None else "expired"  # rows are dropped after a day
        mine = approval is not None and approval.user_id == press.user_id
        if approval is not None and mine and state == "pending":
            approval.state, approval.decided_at = ("approved" if data.run else "denied"), now
            chat = await session.get(Chat, approval.chat_id)
    if state == "expired":  # whoever pressed: the prompt is dead, so it says so and loses its buttons
        await press.edit(t(press.locale, "approvals.expired"))
        return await press.deny("approvals.already_expired")
    if not mine:
        return await press.deny("approvals.not_yours")
    if state != "pending":
        return await press.deny(f"approvals.already_{state}")
    await press.edit(t(press.locale, "approvals.making" if data.run else "approvals.cancelled"))
    await press.toast()  # answered now: the run that follows can take a while
    if data.run and approval is not None and chat is not None:
        await runtime.run_approved(approval, chat, press.locale)


async def close_prompt(
    bot: Bot, approval: Approval, text: str | None = None, keyboard: InlineKeyboardMarkup | None = None
) -> bool:
    """Tidy the prompt once its action is over: deleted when ``text`` is None, else replaced by ``text``.

    False when the prompt couldn't be changed (never stored, deleted, too old), so the caller can post instead.
    """
    if approval.prompt_message_id is None:
        return False
    try:
        if text is None:
            await bot.delete_message(chat_id=approval.chat_id, message_id=approval.prompt_message_id)
        else:
            await bot.edit_message_text(
                chat_id=approval.chat_id,
                message_id=approval.prompt_message_id,
                rich_message=rich(text),
                reply_markup=keyboard,
            )
    except TelegramAPIError:
        return False
    return True


async def close_expired(db: Database, bot: Bot, now: datetime) -> int:
    """Prompts nobody answered in time become the expired notice, without buttons, then are marked expired."""
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Approval, User.language_code)
                .outerjoin(User, User.id == Approval.user_id)
                .where(Approval.state == "pending", Approval.expires_at <= now)
                .order_by(Approval.id)
                .limit(SWEEP)
            )
        ).all()
    for approval, language in rows:  # deleted or too old to edit: nothing left to tidy
        await close_prompt(bot, approval, t(resolve(language), "approvals.expired"))
    if rows:
        async with db.transaction() as session:
            await session.execute(
                update(Approval)
                .where(Approval.id.in_([approval.id for approval, _ in rows]), Approval.state == "pending")
                .values(state="expired")
            )
        log.info("closed %d expired approval prompts", len(rows))
    return len(rows)


async def drop_old(db: Database, before: datetime) -> None:
    """Approvals hold the requested prompt text; they are not kept past a day."""
    async with db.transaction() as session:
        await session.execute(delete(Approval).where(Approval.created_at < before))


def build_router() -> Router:
    router = Router(name="approvals")
    on_press(router, ApprovalAction, F.id > 0)(on_decision)
    return router
