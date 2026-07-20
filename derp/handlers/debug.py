"""Admin diagnostics, including durable one-Star purchase validation."""

from __future__ import annotations

import time

import logfire
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import gettext as _

from derp.billing import (
    DEFAULT_PRODUCT_CATALOG,
    PurchaseIntentService,
    PurchaseTarget,
)
from derp.billing.telegram import PurchaseTargetCode, create_stars_invoice_link
from derp.catalog import GoogleModelKey
from derp.common.sender import MessageSender
from derp.config import settings
from derp.credits import CreditService
from derp.execution import Feature, plan_execution
from derp.history.service import HISTORY_WINDOWS
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception, telemetry_fingerprint

router = Router(name="debug")

# Only process messages from admins
router.message.filter(
    lambda msg: msg.from_user and msg.from_user.id in settings.admin_ids
)
router.callback_query.filter(lambda cb: cb.from_user.id in settings.admin_ids)


class DebugPurchaseCallback(CallbackData, prefix="debug-buy"):
    """Admin-only selector; commercial terms remain in the product catalog."""

    product_id: str
    target: PurchaseTargetCode


@router.message(Command("debug_buy", "dbuy"))
async def debug_buy_command(
    message: Message,
    sender: MessageSender,
    chat_model: ChatModel | None = None,
) -> Message:
    """Present the hidden one-Star product for safe live Stars validation."""
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up
    rows = [
        [
            InlineKeyboardButton(
                text=f"Personal wallet · {product.stars} Star",
                callback_data=DebugPurchaseCallback(
                    product_id=product.id,
                    target=PurchaseTargetCode.USER,
                ).pack(),
            )
        ]
    ]
    if (
        chat_model
        and chat_model.type != "private"
        and chat_model.telegram_id == message.chat.id
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"This chat · {product.stars} Star",
                    callback_data=DebugPurchaseCallback(
                        product_id=product.id,
                        target=PurchaseTargetCode.CHAT,
                    ).pack(),
                )
            ]
        )
    return await sender.reply(
        "<b>Durable Stars test</b>\n"
        f"Buy {product.credits} test credits for {product.stars} Star through "
        "the production intent, pre-checkout, and settlement path.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(DebugPurchaseCallback.filter())
async def handle_debug_buy_callback(
    callback: CallbackQuery,
    callback_data: DebugPurchaseCallback,
    purchase_intents: PurchaseIntentService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> None:
    """Create an opaque durable intent for the selected live wallet target."""
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up
    if not isinstance(callback.message, Message) or not user_model:
        return await callback.answer("Debug purchase is unavailable", show_alert=True)
    if user_model.telegram_id != callback.from_user.id:
        return await callback.answer(
            "Debug purchase identity changed",
            show_alert=True,
        )
    if callback_data.product_id != product.id:
        return await callback.answer(
            "This debug purchase option expired. Run /debug_buy again.",
            show_alert=True,
        )

    if callback_data.target is PurchaseTargetCode.CHAT:
        if (
            not chat_model
            or chat_model.type == "private"
            or chat_model.telegram_id != callback.message.chat.id
        ):
            return await callback.answer(
                "The shared chat target is unavailable",
                show_alert=True,
            )
        target = PurchaseTarget.chat(chat_model.id)
    else:
        target = PurchaseTarget.user(user_model.id)

    try:
        handle = await purchase_intents.create_admin_debug_top_up_intent(
            payer_user_id=user_model.id,
            target=target,
        )
        invoice_link = await create_stars_invoice_link(
            callback.bot,
            handle,
            business_connection_id=callback.message.business_connection_id,
        )
    except LookupError, ValueError:
        return await callback.answer(
            "This debug purchase option is no longer available",
            show_alert=True,
        )
    except TelegramAPIError:
        report_exception(
            "debug_purchase_invoice_link_failed",
            level="warning",
            product_id=product.id,
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            "Telegram could not prepare the debug invoice. Try again.",
            show_alert=True,
        )

    owner = "this chat" if target.kind.value == "chat" else "your wallet"
    await callback.message.answer(
        f"<b>{handle.credits} test credits for {owner}</b>\n"
        "Telegram shows the final one-Star confirmation before charging.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Pay {handle.stars} Star",
                        url=invoice_link,
                    )
                ]
            ]
        ),
    )
    await callback.answer("Debug invoice ready")
    logfire.info(
        "debug_purchase_intent_presented",
        intent_id=str(handle.intent_id),
        product_id=handle.product_id,
        product_version=handle.product_version,
        target=target.kind.value,
        user_id=user_model.telegram_id,
    )


@router.callback_query(F.data.startswith("dbuy:"))
async def reject_legacy_debug_buy_callback(callback: CallbackQuery) -> None:
    """Fail closed for legacy selectors that embedded commercial pack data."""
    await callback.answer(
        "This debug purchase option expired. Run /debug_buy again.",
        show_alert=True,
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

    if target == "chat" and chat_model:
        chat_credits, _ = await credit_service.get_balances(
            user_model.telegram_id,
            chat_model.telegram_id,
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
        _, user_credits = await credit_service.get_balances(
            user_model.telegram_id,
            None,
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
    chat_credits, user_credits = await credit_service.get_balances(
        user_model.telegram_id,
        chat_id,
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
                f"**Admin Policy:** {len(chat_model.admin_policy or '')} chars",
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
        "• /debug_buy - Run a real 1-Star durable purchase\n"
        "• /debug_credits <n> [chat] - Add credits directly\n"
        "• /debug_reset [chat] - Reset credits to 0\n"
        "• /debug_refund <charge_id> - Test refund flow\n\n"
        "**Diagnostics:**\n"
        "• /debug_status - Show model role, balances, and config\n"
        "• /debug_tools - List tools with pricing\n"
        "• /debug_help - This help message"
    )

    return await sender.reply(help_text)
