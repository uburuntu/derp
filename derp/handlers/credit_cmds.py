"""Credit management commands.

Provides commands for users to:
- /credits - Check their credit balance
- /buy - Explain the temporary purchase suspension
"""

from __future__ import annotations

import logfire
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.common.sender import MessageSender
from derp.credits.purchase_suspension import (
    PurchaseIntakeSource,
    reject_purchase_command,
)
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
        return await message.reply(_("😅 Could not find your user info."))

    personal = await operation_ledger.balance(
        WalletOwner(WalletOwnerKind.USER, user_model.id)
    )
    shared = None
    consent_enabled = False
    if chat_model and chat_model.type != "private":
        shared = await operation_ledger.balance(
            WalletOwner(WalletOwnerKind.CHAT, chat_model.id)
        )
        consent_enabled = await operation_ledger.personal_consent_enabled(
            user_model.id, chat_model.id
        )

    logfire.info(
        "credits_checked",
        user_id=user_model.telegram_id,
        chat_id=chat_model and chat_model.telegram_id,
        user_allowance=personal.allowance_available,
        user_purchased=personal.purchased_available,
        chat_purchased=shared and shared.purchased_available,
    )
    text, markup = build_credit_panel(
        personal,
        shared=shared,
        shared_spending_enabled=bool(
            chat_model and chat_model.shared_credit_spending_enabled
        ),
        personal_fallback_enabled=consent_enabled,
    )
    return await sender.reply(text, reply_markup=markup)


@router.message(Command("buy", "purchase", "shop"))
async def show_buy_options(
    message: Message,
    sender: MessageSender,
) -> Message:
    """Reject new personal credit purchases until durable intents ship."""
    return await reject_purchase_command(
        message,
        sender,
        PurchaseIntakeSource.PERSONAL_COMMAND,
    )


@router.message(Command("buy_chat", "buychat"))
async def show_buy_chat_options(
    message: Message,
    sender: MessageSender,
) -> Message:
    """Reject new chat credit purchases until durable intents ship."""
    return await reject_purchase_command(
        message,
        sender,
        PurchaseIntakeSource.CHAT_COMMAND,
    )
