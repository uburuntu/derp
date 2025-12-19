"""Credit management commands.

Provides commands for users to:
- /credits - Check their credit balance
- /buy - Purchase credits with Telegram Stars
"""

from __future__ import annotations

import logfire
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.common.sender import MessageSender
from derp.credits import CreditService
from derp.credits.packs import CREDIT_PACKS
from derp.credits.ui import build_buy_keyboard
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
            parts.append(_("💡 No credits! Use /buy to get some."))
    else:
        parts.append(
            _("👤 Balance: **{credits}** credits\n").format(credits=user_credits)
        )
        if user_credits > 0:
            parts.append(_("✅ You have credits for premium features!"))
        else:
            parts.append(_("💡 No credits! Use /buy to get some."))

    return await sender.reply("\n".join(parts))


@router.message(Command("buy", "purchase", "shop"))
async def show_buy_options(
    message: Message,
    sender: MessageSender,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Show credit purchase options with inline payment buttons.

    Displays available credit packs and inline buttons to purchase.
    Users can buy credits for themselves or for the chat pool.
    """
    if not user_model:
        return await message.reply(_("😅 Could not find your user info."))

    logfire.info(
        "buy_menu_shown",
        user_id=user_model.telegram_id,
        chat_id=chat_model and chat_model.telegram_id,
    )

    # Build message with pack info
    parts = [
        _("🛒 **Credit Packs**\n"),
        _("Buy credits with Telegram Stars ⭐\n"),
    ]

    for pack in CREDIT_PACKS.values():
        if pack.bonus_pct > 0:
            parts.append(
                _(
                    "• **{name}**: {stars} ⭐ → {credits} credits (+{bonus}% bonus)"
                ).format(
                    name=pack.name,
                    stars=pack.stars,
                    credits=pack.credits,
                    bonus=pack.bonus_pct,
                )
            )
        else:
            parts.append(
                _("• **{name}**: {stars} ⭐ → {credits} credits").format(
                    name=pack.name, stars=pack.stars, credits=pack.credits
                )
            )

    parts.extend(
        [
            "",
            _("**What can you do with credits?**"),
            _("• 1 credit = 1 AI message (better quality model)"),
            _("• 5 credits = 1 image generation"),
            _("• 10 credits = 1 deep thinking (/think)"),
            "",
            _("👇 **Tap a button to buy:**"),
        ]
    )

    # Build keyboard - personal credits by default
    # In group chats, also offer chat credits option
    keyboard = build_buy_keyboard(chat_id=None)  # Personal credits

    return await sender.reply(
        "\n".join(parts),
        reply_markup=keyboard,
    )


@router.message(Command("buy_chat", "buychat"))
async def show_buy_chat_options(
    message: Message,
    sender: MessageSender,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Buy credits for the chat pool (group chats only).

    Chat credits are shared among all members and used first.
    """
    if not user_model:
        return await message.reply(_("😅 Could not find your user info."))

    if not chat_model or chat_model.type == "private":
        return await message.reply(
            _(
                "💡 This command is for group chats only.\nUse /buy for personal credits."
            )
        )

    logfire.info(
        "buy_chat_menu_shown",
        user_id=user_model.telegram_id,
        chat_id=chat_model.telegram_id,
    )

    parts = [
        _("🏠 **Buy Chat Credits**\n"),
        _("Credits for this chat's shared pool.\n"),
        _("Everyone in the chat can use them!\n"),
        "",
        _("👇 **Tap a button to buy:**"),
    ]

    keyboard = build_buy_keyboard(chat_id=chat_model.telegram_id)

    return await sender.reply(
        "\n".join(parts),
        reply_markup=keyboard,
    )
