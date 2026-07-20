"""Admin diagnostics and reconciliation for legacy debug payments."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import logfire
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from pydantic import BaseModel, ConfigDict, Field

from derp.catalog import GoogleModelKey
from derp.common.sender import MessageSender
from derp.config import settings
from derp.credits import CreditService
from derp.credits.purchase_suspension import (
    PurchaseIntakeSource,
    reject_purchase_callback,
    reject_purchase_command,
    reject_purchase_pre_checkout,
)
from derp.db.credits import get_balances
from derp.execution import Feature, plan_execution
from derp.history.service import HISTORY_WINDOWS
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception, telemetry_fingerprint

router = Router(name="debug")
reconciliation_router = Router(name="debug_payment_reconciliation")

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


@router.message(Command("debug_buy", "dbuy"))
async def debug_buy_command(
    message: Message,
    sender: MessageSender,
) -> Message:
    """Reject new debug purchases while preserving reconciliation."""
    return await reject_purchase_command(
        message,
        sender,
        PurchaseIntakeSource.DEBUG_COMMAND,
    )


@router.callback_query(F.data.startswith("dbuy:"))
async def handle_debug_buy_callback(
    callback: CallbackQuery,
) -> None:
    """Reject stale debug purchase buttons without creating an invoice."""
    await reject_purchase_callback(callback, PurchaseIntakeSource.DEBUG_CALLBACK)


@router.pre_checkout_query(F.invoice_payload.contains('"k":"debug_credits"'))
async def handle_debug_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
    """Reject legacy debug invoices before Telegram captures Stars."""
    await reject_purchase_pre_checkout(
        pre_checkout,
        PurchaseIntakeSource.DEBUG_PRE_CHECKOUT,
    )


@reconciliation_router.message(
    F.successful_payment,
    F.successful_payment.invoice_payload.contains('"k":"debug_credits"'),
)
async def handle_debug_successful_payment(
    message: Message,
    sender: MessageSender,
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
        report_exception("debug_payload_decode_failed")
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
            await sender.send(
                f"✅ **DEBUG Payment OK**\n\n"
                f"Added **{pack.credits}** credits to chat.\n"
                f"New chat balance: **{new_balance}** credits\n"
                f"Charge ID: `{payment.telegram_payment_charge_id}`",
            )
        else:
            new_balance = await credit_service.purchase_credits(
                user_model,
                None,
                pack.credits,
                payment.telegram_payment_charge_id,
                pack_name=f"DEBUG:{pack.name}",
            )
            await sender.send(
                f"✅ **DEBUG Payment OK**\n\n"
                f"Added **{pack.credits}** credits to your account.\n"
                f"New balance: **{new_balance}** credits\n"
                f"Charge ID: `{payment.telegram_payment_charge_id}`",
            )

        logfire.info(
            "debug_payment_processed",
            pack_id=pack.id,
            credits=pack.credits,
            user_id=user_model.telegram_id,
            target_type=payload.target_type,
            charge_fingerprint=telemetry_fingerprint(
                payment.telegram_payment_charge_id
            ),
        )

    except Exception:
        report_exception("debug_payment_processing_failed")
        await sender.send(
            f"❌ Payment processing failed.\n"
            f"Charge ID: `{payment.telegram_payment_charge_id}`",
        )


@router.message(Command("debug_credits", "dcredits"))
async def debug_add_credits(
    message: Message,
    sender: MessageSender,
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
                charge_fingerprint=telemetry_fingerprint(fake_charge_id),
            )
            return await sender.reply(
                f"✅ Added **{amount}** credits to chat.\n"
                f"New balance: **{new_balance}**\n"
                f"Charge ID: `{fake_charge_id}`",
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
                charge_fingerprint=telemetry_fingerprint(fake_charge_id),
            )
            return await sender.reply(
                f"✅ Added **{amount}** credits to your account.\n"
                f"New balance: **{new_balance}**\n"
                f"Charge ID: `{fake_charge_id}`",
            )
    except Exception:
        report_exception("debug_credits_failed")
        return await message.reply("❌ Failed to add credits")


@router.message(Command("debug_reset", "dreset"))
async def debug_reset_credits(
    message: Message,
    sender: MessageSender,
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
        return await sender.reply(
            f"✅ Chat credits reset.\nPrevious: **{chat_credits}** → Now: **0**",
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
        return await sender.reply(
            f"✅ Your credits reset.\nPrevious: **{user_credits}** → Now: **0**",
        )


@router.message(Command("debug_status", "dstatus"))
async def debug_status(
    message: Message,
    sender: MessageSender,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Show detailed debug status for credits and model selection.

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

    # The shared-chat path requires both database models for balance policy.
    if chat_model:
        plan, history_window = await credit_service.get_orchestrator_config(
            user_model, chat_model
        )
    else:
        plan = plan_execution(
            Feature.CHAT,
            GoogleModelKey.CHAT_ECONOMY
            if user_credits == 0
            else GoogleModelKey.CHAT_STANDARD,
        )
        history_window = HISTORY_WINDOWS[plan.model.key]
    model = plan.model
    is_paid = (chat_credits + user_credits) > 0

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
            "**Model Selection:**",
            f"• Role: `{model.key.value}`",
            f"• Model: `{model.provider_model_id}`",
            f"• Context: {history_window.max_turns} turns / "
            f"{history_window.max_tokens} estimated tokens",
            "",
            f"**Is Paid Tier:** {'✅ Yes' if is_paid else '❌ No (free)'}",
            f"**Premium Tools:** {'✅ Available' if is_paid else '❌ Not available'}",
        ]
    )

    logfire.info(
        "debug_status_shown",
        user_id=user_model.telegram_id,
        chat_id=chat_id,
        model_key=model.key.value,
        model=model.provider_model_id,
        user_credits=user_credits,
        chat_credits=chat_credits,
    )

    return await sender.reply("\n".join(lines))


@router.message(Command("debug_refund", "drefund"))
async def debug_refund(
    message: Message,
    sender: MessageSender,
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
        logfire.info(
            "debug_refund_success",
            charge_fingerprint=telemetry_fingerprint(charge_id),
        )
        return await sender.reply(
            f"✅ Refund processed for `{charge_id}`",
        )
    else:
        logfire.warn(
            "debug_refund_failed",
            charge_fingerprint=telemetry_fingerprint(charge_id),
        )
        return await sender.reply(
            f"❌ Refund failed for `{charge_id}`\n"
            f"Transaction not found or already refunded.",
        )


@router.message(Command("debug_tools", "dtools"))
async def debug_tools(
    message: Message,
    sender: MessageSender,
) -> Message:
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

    return await sender.reply("\n".join(lines))


@router.message(Command("debug_help", "dhelp"))
async def debug_help(
    message: Message,
    sender: MessageSender,
) -> Message:
    """Show all debug commands.

    Usage:
    - /debug_help - Show this help
    """
    help_text = _(
        "🛠 **Debug Commands** (admin only)\n\n"
        "**Credit Testing:**\n"
        "• /debug_credits <n> [chat] - Add credits directly\n"
        "• /debug_reset [chat] - Reset credits to 0\n"
        "• /debug_refund <charge_id> - Test refund flow\n\n"
        "**Diagnostics:**\n"
        "• /debug_status - Show model role, balances, and config\n"
        "• /debug_tools - List tools with pricing\n"
        "• /debug_help - This help message"
    )

    return await sender.reply(help_text)
