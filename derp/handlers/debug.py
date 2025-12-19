"""Debug commands for admin-only production testing.

These commands are thin proxies to real handlers to ensure production code paths
are tested. They use reduced pricing (1 star) for safe payment testing.

Only available to admins defined in settings.admin_ids.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import logfire
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.i18n import gettext as _
from pydantic import BaseModel, ConfigDict, Field

from derp.config import settings
from derp.credits import CreditService, ModelTier
from derp.db.credits import get_balances
from derp.models import Chat as ChatModel
from derp.models import User as UserModel

router = Router(name="debug")

# Only process messages from admins
router.message.filter(
    lambda msg: msg.from_user and msg.from_user.id in settings.admin_ids
)
router.callback_query.filter(lambda cb: cb.from_user.id in settings.admin_ids)


# --- Debug Credit Packs (1 star each for testing) ---
@dataclass(frozen=True, slots=True)
class DebugCreditPack:
    """A debug credit pack with minimal cost."""

    id: str
    name: str
    stars: int  # Always 1 for testing
    credits: int


DEBUG_PACKS: dict[str, DebugCreditPack] = {
    "test_small": DebugCreditPack("test_small", "Test Small", 1, 10),
    "test_medium": DebugCreditPack("test_medium", "Test Medium", 1, 50),
    "test_large": DebugCreditPack("test_large", "Test Large", 1, 100),
}


class DebugPayload(BaseModel):
    """Validated payload for debug invoices."""

    kind: Literal["debug_credits"] = Field(default="debug_credits", alias="k")
    pack_id: str = Field(alias="p")
    target_type: Literal["user", "chat"] = Field(alias="tt")
    target_id: int = Field(alias="ti")

    model_config = ConfigDict(populate_by_name=True)


def _build_debug_buy_keyboard(chat_id: int | None = None) -> InlineKeyboardMarkup:
    """Build inline keyboard with debug buy buttons."""
    buttons = []

    # User credits buttons
    for pack in DEBUG_PACKS.values():
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {pack.stars}⭐ → {pack.credits} credits (user)",
                    callback_data=f"dbuy:{pack.id}:user",
                )
            ]
        )

    # Chat credits buttons (if in a chat)
    if chat_id:
        for pack in DEBUG_PACKS.values():
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"💬 {pack.stars}⭐ → {pack.credits} credits (chat)",
                        callback_data=f"dbuy:{pack.id}:chat:{chat_id}",
                    )
                ]
            )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("debug_buy", "dbuy"))
async def debug_buy_command(
    message: Message,
    chat_model: ChatModel | None = None,
) -> Message:
    """Show debug credit purchase options (1 star each).

    Usage:
    - /debug_buy - Shows test packs for both user and chat credits
    """
    chat_id = chat_model.telegram_id if chat_model else None

    text = _(
        "🛠 **Debug Buy Menu**\n\n"
        "All packs cost 1⭐ for testing.\n"
        "Choose user or chat credits:"
    )

    return await message.reply(
        text,
        parse_mode="Markdown",
        reply_markup=_build_debug_buy_keyboard(chat_id),
    )


@router.callback_query(F.data.startswith("dbuy:"))
async def handle_debug_buy_callback(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    """Handle debug buy button press - create invoice with 1 star."""
    if not callback.data or not callback.message:
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Invalid debug purchase request", show_alert=True)
        return

    pack_id = parts[1]
    target_type = parts[2]

    pack = DEBUG_PACKS.get(pack_id)
    if not pack:
        await callback.answer("Unknown debug pack", show_alert=True)
        return

    # Build payload
    if target_type == "chat" and len(parts) >= 4:
        target_id = int(parts[3])
        payload = DebugPayload(
            pack_id=pack_id,
            target_type="chat",
            target_id=target_id,
        )
        description = f"DEBUG: {pack.credits} credits for chat {target_id}"
    else:
        target_id = callback.from_user.id
        payload = DebugPayload(
            pack_id=pack_id,
            target_type="user",
            target_id=target_id,
        )
        description = f"DEBUG: {pack.credits} credits for user"

    logfire.info(
        "debug_invoice_created",
        pack_id=pack_id,
        target_type=target_type,
        target_id=target_id,
        user_id=callback.from_user.id,
    )

    # Send invoice directly (1 star)
    try:
        await callback.message.answer_invoice(
            title=f"🛠 Debug: {pack.name}",
            description=description,
            payload=payload.model_dump_json(by_alias=True, exclude_none=True),
            currency="XTR",
            prices=[LabeledPrice(label="Debug Credits", amount=pack.stars)],
        )
        await callback.answer()
    except Exception:
        logfire.exception("debug_invoice_failed", pack_id=pack_id)
        await callback.answer("Failed to create invoice", show_alert=True)


@router.pre_checkout_query(F.invoice_payload.contains('"k":"debug_credits"'))
async def handle_debug_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
    """Approve debug payment pre-checkout."""
    logfire.info(
        "debug_pre_checkout_ok",
        payload=pre_checkout.invoice_payload,
        total_amount=pre_checkout.total_amount,
        user_id=pre_checkout.from_user.id,
    )
    await pre_checkout.answer(ok=True)


@router.message(
    F.successful_payment,
    F.successful_payment.invoice_payload.contains('"k":"debug_credits"'),
)
async def handle_debug_successful_payment(
    message: Message,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> None:
    """Handle successful debug payment - add credits via real CreditService."""
    if not message.successful_payment or not message.from_user:
        return

    payment = message.successful_payment

    try:
        payload = DebugPayload.model_validate_json(payment.invoice_payload)
    except Exception:
        logfire.exception(
            "debug_payload_decode_failed", payload=payment.invoice_payload
        )
        await message.answer("❌ Failed to decode payment payload")
        return

    pack = DEBUG_PACKS.get(payload.pack_id)
    if not pack:
        logfire.error("debug_unknown_pack", pack_id=payload.pack_id)
        await message.answer("❌ Unknown debug pack")
        return

    if not user_model:
        logfire.error("debug_no_user", user_id=message.from_user.id)
        await message.answer("❌ User not found")
        return

    try:
        if payload.target_type == "chat" and chat_model:
            new_balance = await credit_service.purchase_credits(
                user_model,
                chat_model,
                pack.credits,
                payment.telegram_payment_charge_id,
                pack_name=f"DEBUG:{pack.name}",
            )
            await message.answer(
                f"✅ **DEBUG Payment OK**\n\n"
                f"Added **{pack.credits}** credits to chat.\n"
                f"New chat balance: **{new_balance}** credits\n"
                f"Charge ID: `{payment.telegram_payment_charge_id}`",
                parse_mode="Markdown",
            )
        else:
            new_balance = await credit_service.purchase_credits(
                user_model,
                None,
                pack.credits,
                payment.telegram_payment_charge_id,
                pack_name=f"DEBUG:{pack.name}",
            )
            await message.answer(
                f"✅ **DEBUG Payment OK**\n\n"
                f"Added **{pack.credits}** credits to your account.\n"
                f"New balance: **{new_balance}** credits\n"
                f"Charge ID: `{payment.telegram_payment_charge_id}`",
                parse_mode="Markdown",
            )

        logfire.info(
            "debug_payment_processed",
            pack_id=pack.id,
            credits=pack.credits,
            user_id=user_model.telegram_id,
            target_type=payload.target_type,
            charge_id=payment.telegram_payment_charge_id,
        )

    except Exception:
        logfire.exception("debug_payment_processing_failed")
        await message.answer(
            f"❌ Payment processing failed.\n"
            f"Charge ID: `{payment.telegram_payment_charge_id}`",
            parse_mode="Markdown",
        )


@router.message(Command("debug_credits", "dcredits"))
async def debug_add_credits(
    message: Message,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Directly add credits without payment (for testing).

    Usage:
    - /debug_credits 100 - Add 100 credits to your account
    - /debug_credits 100 chat - Add 100 credits to this chat
    """
    if not user_model:
        return await message.reply("❌ User not found")

    args = (message.text or "").split()[1:] if message.text else []
    amount = 100  # Default
    target = "user"

    if args:
        try:
            amount = int(args[0])
        except ValueError:
            return await message.reply(
                "❌ Invalid amount. Usage: /debug_credits <amount> [chat]"
            )

        if len(args) > 1 and args[1].lower() == "chat":
            target = "chat"

    if target == "chat" and not chat_model:
        return await message.reply("❌ Not in a chat context")

    fake_charge_id = f"debug-{int(time.time())}"

    try:
        if target == "chat" and chat_model:
            new_balance = await credit_service.purchase_credits(
                user_model,
                chat_model,
                amount,
                fake_charge_id,
                pack_name="DEBUG:manual",
            )
            logfire.info(
                "debug_credits_added",
                amount=amount,
                target="chat",
                chat_id=chat_model.telegram_id,
                charge_id=fake_charge_id,
            )
            return await message.reply(
                f"✅ Added **{amount}** credits to chat.\n"
                f"New balance: **{new_balance}**\n"
                f"Charge ID: `{fake_charge_id}`",
                parse_mode="Markdown",
            )
        else:
            new_balance = await credit_service.purchase_credits(
                user_model,
                None,
                amount,
                fake_charge_id,
                pack_name="DEBUG:manual",
            )
            logfire.info(
                "debug_credits_added",
                amount=amount,
                target="user",
                user_id=user_model.telegram_id,
                charge_id=fake_charge_id,
            )
            return await message.reply(
                f"✅ Added **{amount}** credits to your account.\n"
                f"New balance: **{new_balance}**\n"
                f"Charge ID: `{fake_charge_id}`",
                parse_mode="Markdown",
            )
    except Exception:
        logfire.exception("debug_credits_failed")
        return await message.reply("❌ Failed to add credits")


@router.message(Command("debug_reset", "dreset"))
async def debug_reset_credits(
    message: Message,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Reset credits to 0 for testing.

    Usage:
    - /debug_reset - Reset your credits to 0
    - /debug_reset chat - Reset chat credits to 0
    """
    if not user_model:
        return await message.reply("❌ User not found")

    args = (message.text or "").split()[1:] if message.text else []
    target = "user"

    if args and args[0].lower() == "chat":
        target = "chat"

    if target == "chat" and not chat_model:
        return await message.reply("❌ Not in a chat context")

    # Get current balance first (use credit_service.session for raw query)
    if target == "chat" and chat_model:
        chat_credits, _ = await get_balances(
            credit_service.session, user_model.telegram_id, chat_model.telegram_id
        )
        if chat_credits > 0:
            fake_charge_id = f"debug-reset-{int(time.time())}"
            await credit_service.purchase_credits(
                user_model,
                chat_model,
                -chat_credits,  # Negative to subtract
                fake_charge_id,
                pack_name="DEBUG:reset",
            )
        logfire.info(
            "debug_credits_reset",
            target="chat",
            chat_id=chat_model.telegram_id,
            previous_balance=chat_credits,
        )
        return await message.reply(
            f"✅ Chat credits reset.\nPrevious: **{chat_credits}** → Now: **0**",
            parse_mode="Markdown",
        )
    else:
        _, user_credits = await get_balances(
            credit_service.session, user_model.telegram_id, None
        )
        if user_credits > 0:
            fake_charge_id = f"debug-reset-{int(time.time())}"
            await credit_service.purchase_credits(
                user_model,
                None,
                -user_credits,
                fake_charge_id,
                pack_name="DEBUG:reset",
            )
        logfire.info(
            "debug_credits_reset",
            target="user",
            user_id=user_model.telegram_id,
            previous_balance=user_credits,
        )
        return await message.reply(
            f"✅ Your credits reset.\nPrevious: **{user_credits}** → Now: **0**",
            parse_mode="Markdown",
        )


@router.message(Command("debug_status", "dstatus"))
async def debug_status(
    message: Message,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Show detailed debug status for credits and tier detection.

    Usage:
    - /debug_status - Show full diagnostic info
    """
    if not user_model:
        return await message.reply("❌ User not found")

    # Get balances
    chat_id = chat_model.telegram_id if chat_model else None
    chat_credits, user_credits = await get_balances(
        credit_service.session, user_model.telegram_id, chat_id
    )

    # Get orchestrator config (requires both models for the new API)
    if chat_model:
        tier, model_id, context_limit = await credit_service.get_orchestrator_config(
            user_model, chat_model
        )
    else:
        # No chat model, use defaults
        tier = ModelTier.CHEAP if user_credits == 0 else ModelTier.STANDARD
        model_id = "default"
        context_limit = 10 if tier == ModelTier.CHEAP else 100

    # Build status report
    lines = [
        "🛠 **Debug Status**\n",
        f"**User ID:** `{user_model.telegram_id}`",
        f"**User DB ID:** `{user_model.id}`",
        f"**User Credits:** {user_credits}",
    ]

    if chat_model:
        lines.extend(
            [
                "",
                f"**Chat ID:** `{chat_model.telegram_id}`",
                f"**Chat DB ID:** `{chat_model.id}`",
                f"**Chat Credits:** {chat_credits}",
                f"**Chat Memory:** {len(chat_model.llm_memory or '')} chars",
            ]
        )

    lines.extend(
        [
            "",
            "**Tier Detection:**",
            f"• Tier: `{tier.value}`",
            f"• Model: `{model_id}`",
            f"• Context Limit: {context_limit} messages",
            "",
            f"**Is Paid Tier:** {'✅ Yes' if (chat_credits + user_credits) > 0 else '❌ No (free)'}",
            f"**Premium Tools:** {'✅ Available' if tier != ModelTier.CHEAP else '❌ Not available'}",
        ]
    )

    logfire.info(
        "debug_status_shown",
        user_id=user_model.telegram_id,
        chat_id=chat_id,
        tier=tier.value,
        user_credits=user_credits,
        chat_credits=chat_credits,
    )

    return await message.reply("\n".join(lines), parse_mode="Markdown")


@router.message(Command("debug_refund", "drefund"))
async def debug_refund(
    message: Message,
    credit_service: CreditService,
) -> Message:
    """Test refund flow with a charge ID.

    Usage:
    - /debug_refund <charge_id> - Attempt to refund a transaction
    """
    args = (message.text or "").split()[1:] if message.text else []
    if not args:
        return await message.reply(
            "❌ Usage: /debug_refund <charge_id>\n\n"
            "Get charge IDs from /debug_status or payment confirmations."
        )

    charge_id = args[0]

    success = await credit_service.refund_credits(charge_id)

    if success:
        logfire.info("debug_refund_success", charge_id=charge_id)
        return await message.reply(
            f"✅ Refund processed for `{charge_id}`",
            parse_mode="Markdown",
        )
    else:
        logfire.warn("debug_refund_failed", charge_id=charge_id)
        return await message.reply(
            f"❌ Refund failed for `{charge_id}`\n"
            f"Transaction not found or already refunded.",
            parse_mode="Markdown",
        )


@router.message(Command("debug_tools", "dtools"))
async def debug_tools(message: Message) -> Message:
    """List available tools with their credit costs.

    Usage:
    - /debug_tools - Show all tools and their pricing
    """
    from derp.credits.tools import TOOL_REGISTRY

    lines = ["🛠 **Available Tools**\n"]

    for tool_id, tool in TOOL_REGISTRY.items():
        premium_badge = "💎" if tool.is_premium else "🆓"
        lines.append(
            f"{premium_badge} **{tool.name}** (`{tool_id}`)\n"
            f"   Cost: {tool.base_credit_cost} credits | "
            f"Free daily: {tool.free_daily_limit}\n"
            f"   {tool.description}"
        )

    return await message.reply("\n".join(lines), parse_mode="Markdown")


@router.message(Command("debug_help", "dhelp"))
async def debug_help(message: Message) -> Message:
    """Show all debug commands.

    Usage:
    - /debug_help - Show this help
    """
    help_text = _(
        "🛠 **Debug Commands** (admin only)\n\n"
        "**Payment Testing:**\n"
        "• /debug_buy - Buy credits with 1⭐ test packs\n"
        "• /debug_credits <n> [chat] - Add credits directly\n"
        "• /debug_reset [chat] - Reset credits to 0\n"
        "• /debug_refund <charge_id> - Test refund flow\n\n"
        "**Diagnostics:**\n"
        "• /debug_status - Show tier, balances, model config\n"
        "• /debug_tools - List tools with pricing\n"
        "• /debug_help - This help message"
    )

    return await message.reply(help_text, parse_mode="Markdown")
