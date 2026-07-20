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
from derp.credits import CreditService
from derp.credits.purchase_suspension import (
    PurchaseIntakeSource,
    reject_purchase_command,
)
from derp.db.credits import get_balances
from derp.models import Chat as ChatModel
from derp.models import User as UserModel

router = Router(name="credit_cmds")


@router.message(Command("credits", "balance", "bal"))
async def show_credits(
    message: Message,
    sender: MessageSender,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Show the user's credit balance."""
    if not user_model:
        return await message.reply(_("😅 Could not find your user info."))

    if chat_model:
        chat_credits, user_credits = await get_balances(
            credit_service.session, user_model.telegram_id, chat_model.telegram_id
        )
    else:
        chat_credits, user_credits = await get_balances(
            credit_service.session, user_model.telegram_id, None
        )
        chat_credits = 0  # No chat context

    logfire.info(
        "credits_checked",
        user_id=user_model.telegram_id,
        chat_id=chat_model and chat_model.telegram_id,
        user_credits=user_credits,
        chat_credits=chat_credits,
    )

    # Build response message
    parts = [_("💰 **Your Credits**\n")]

    if chat_model and chat_model.type != "private":
        parts.append(
            _("🏠 Chat pool: **{credits}** credits").format(credits=chat_credits)
        )
        parts.append(
            _("👤 Personal: **{credits}** credits\n").format(credits=user_credits)
        )
        if chat_credits > 0:
            parts.append(_("✅ Chat credits will be used first."))
        elif user_credits > 0:
            parts.append(_("✅ Your personal credits will be used."))
        else:
            parts.append(_("No credits available. Free features still work."))
    else:
        parts.append(
            _("👤 Balance: **{credits}** credits\n").format(credits=user_credits)
        )
        if user_credits > 0:
            parts.append(_("✅ You have credits for premium features!"))
        else:
            parts.append(_("No credits available. Free features still work."))

    return await sender.reply("\n".join(parts))


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
