"""Credit management commands.

Provides commands for users to:
- /credits - Check their credit balance
- /buy - Add personal credits or fund the current shared chat
"""

from __future__ import annotations

import logfire
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import gettext as _

from derp.billing import CLOSED_COMMERCE_POLICY, CommercePolicy
from derp.billing.telegram import (
    PurchaseTargetCallback,
    PurchaseTargetCode,
    build_purchase_panel,
    build_purchase_target_panel,
)
from derp.common.private_delivery import deliver_sensitive_reply
from derp.common.sender import MessageSender
from derp.credits.purchase_suspension import purchase_suspension_message
from derp.handlers.context_settings import build_credit_panel
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.operations import OperationLedger, WalletOwner, WalletOwnerKind

router = Router(name="credit_cmds")


@router.message(Command("credits", "balance", "bal"))
async def show_credits(
    message: Message,
    sender: MessageSender,
    operation_ledger: OperationLedger,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Show the user's credit balance."""
    if not user_model:
        return await message.reply(_("I couldn't find your account. Try again."))

    personal = await operation_ledger.statement(
        WalletOwner(WalletOwnerKind.USER, user_model.id)
    )
    shared = None
    consent_enabled = False
    if chat_model and chat_model.type != "private":
        shared = await operation_ledger.statement(
            WalletOwner(WalletOwnerKind.CHAT, chat_model.id)
        )
        consent_enabled = await operation_ledger.personal_consent_enabled(
            user_model.id, chat_model.id
        )

    logfire.info(
        "credits_checked",
        user_id=user_model.telegram_id,
        chat_id=chat_model and chat_model.telegram_id,
        has_personal_balance=personal.balance.spendable > 0,
        has_shared_balance=bool(shared and shared.balance.spendable > 0),
    )
    text, markup = build_credit_panel(
        personal,
        shared=shared,
        shared_spending_enabled=bool(
            chat_model and chat_model.shared_credit_spending_enabled
        ),
        personal_fallback_enabled=consent_enabled,
    )
    return await deliver_sensitive_reply(
        message,
        sender,
        text,
        recipient_chat_id=user_model.telegram_id,
        public_success=_("I sent your credit details in a private chat."),
        public_failure=_(
            "I couldn't send your credit details. Open Derp privately and use /credits."
        ),
        failure_event="private_credit_delivery_failed",
        reply_markup=markup,
    )


@router.message(Command("buy", "purchase", "shop"))
async def show_buy_options(
    message: Message,
    sender: MessageSender,
    user_model: UserModel | None = None,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
) -> Message:
    """Show durable personal top-ups and the single recurring plan."""
    if not commerce_policy.public_intake_enabled:
        return await sender.reply(purchase_suspension_message())
    if not user_model:
        return await message.reply(_("I couldn't find your account. Try again."))
    if message.chat.type != "private":
        text, markup = build_purchase_target_panel(
            actor_telegram_id=user_model.telegram_id
        )
        return await sender.reply(text, reply_markup=markup)
    text, markup = build_purchase_panel(
        target=PurchaseTargetCode.USER,
        actor_telegram_id=user_model.telegram_id,
    )
    return await sender.reply(text, reply_markup=markup)


@router.message(Command("buy_chat", "buychat"))
async def redirect_legacy_buy_chat(sender: MessageSender) -> Message:
    """Keep the retired command from falling through to paid chat inference."""
    return await sender.reply(_("Use /buy. I'll show the right options here."))


@router.callback_query(PurchaseTargetCallback.filter())
async def choose_purchase_target(
    query: CallbackQuery,
    callback_data: PurchaseTargetCallback,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
) -> None:
    """Resolve a contextual group target without exposing a second command."""
    if not commerce_policy.public_intake_enabled:
        await query.answer(
            _("Credit purchases aren't available yet. You won't be charged."),
            show_alert=True,
        )
        return
    if (
        not isinstance(query.message, Message)
        or user_model is None
        or user_model.telegram_id != query.from_user.id
        or callback_data.actor_id != query.from_user.id
    ):
        await query.answer(
            _("This purchase menu belongs to someone else."), show_alert=True
        )
        return
    if callback_data.target is PurchaseTargetCode.CHAT and (
        chat_model is None or chat_model.type == "private"
    ):
        await query.answer(
            _("This chat can't receive shared credits."), show_alert=True
        )
        return
    text, markup = build_purchase_panel(
        target=callback_data.target,
        actor_telegram_id=callback_data.actor_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()
