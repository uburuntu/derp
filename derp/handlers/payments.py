"""Telegram Stars payment handler.

Handles credit purchases via Telegram Stars (XTR currency).
Flow:
1. User clicks a buy button with invoice link
2. Pre-checkout query is answered (approve payment)
3. Successful payment triggers credit addition
"""

from __future__ import annotations

from dataclasses import dataclass

import logfire
from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.i18n import gettext as _

from derp.credits import CreditService
from derp.db import get_db_manager
from derp.models import Chat as ChatModel
from derp.models import User as UserModel

router = Router(name="payments")


# Credit pack definitions
@dataclass(frozen=True, slots=True)
class CreditPack:
    """A purchasable credit pack."""

    id: str
    name: str
    stars: int  # Telegram Stars cost
    credits: int  # Credits received
    bonus_pct: int  # Bonus percentage for display


CREDIT_PACKS: dict[str, CreditPack] = {
    "starter": CreditPack("starter", "Starter", 50, 50, 0),
    "basic": CreditPack("basic", "Basic", 150, 165, 10),
    "standard": CreditPack("standard", "Standard", 500, 600, 20),
    "bulk": CreditPack("bulk", "Bulk", 1500, 2000, 33),
}


def build_buy_keyboard(chat_id: int | None = None) -> InlineKeyboardMarkup:
    """Build inline keyboard with buy buttons.

    Args:
        chat_id: If provided, credits go to chat pool. Otherwise personal.
    """
    target = f"chat:{chat_id}" if chat_id else "user"

    buttons = []
    for pack in CREDIT_PACKS.values():
        if pack.bonus_pct > 0:
            label = f"⭐ {pack.stars} → {pack.credits} credits (+{pack.bonus_pct}%)"
        else:
            label = f"⭐ {pack.stars} → {pack.credits} credits"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"buy:{pack.id}:{target}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("buy:"))
async def handle_buy_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: UserModel | None = None,
    chat_settings: ChatModel | None = None,
) -> None:
    """Handle buy button press - create and send invoice."""
    if not callback.data or not callback.message:
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer(_("Invalid purchase request"), show_alert=True)
        return

    pack_id = parts[1]
    target = parts[2]

    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        await callback.answer(_("Unknown credit pack"), show_alert=True)
        return

    # Build payload with target info
    # Format: pack_id:target_type:target_id
    if target.startswith("chat:"):
        chat_id = target.split(":")[1]
        payload = f"{pack_id}:chat:{chat_id}"
        description = _("{credits} credits for this chat").format(credits=pack.credits)
    else:
        payload = f"{pack_id}:user:{callback.from_user.id}"
        description = _("{credits} credits for your account").format(
            credits=pack.credits
        )

    logfire.info(
        "invoice_created",
        pack_id=pack_id,
        target=target,
        user_id=callback.from_user.id,
        stars=pack.stars,
        credits=pack.credits,
    )

    # Create invoice link
    try:
        invoice_link = await bot.create_invoice_link(
            title=_("{name} Credit Pack").format(name=pack.name),
            description=description,
            payload=payload,
            currency="XTR",  # Telegram Stars
            prices=[LabeledPrice(label=_("Credits"), amount=pack.stars)],
            provider_token="",  # Empty for Stars
        )

        await callback.message.answer(
            _("💫 Click below to complete your purchase:\n\n{link}").format(
                link=invoice_link
            ),
        )
        await callback.answer()

    except Exception:
        logfire.exception("invoice_creation_failed", pack_id=pack_id)
        await callback.answer(
            _("Failed to create invoice. Try again."), show_alert=True
        )


@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
    """Answer pre-checkout query to approve payment.

    This is called by Telegram before the payment is processed.
    We should validate the purchase and respond within 10 seconds.
    """
    payload = pre_checkout.invoice_payload
    parts = payload.split(":")

    if len(parts) < 3:
        await pre_checkout.answer(ok=False, error_message=_("Invalid payment data"))
        return

    pack_id = parts[0]
    if pack_id not in CREDIT_PACKS:
        await pre_checkout.answer(ok=False, error_message=_("Unknown credit pack"))
        return

    logfire.info(
        "pre_checkout_approved",
        pack_id=pack_id,
        user_id=pre_checkout.from_user.id,
        total_amount=pre_checkout.total_amount,
    )

    # Approve the payment
    await pre_checkout.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(
    message: Message,
    user: UserModel | None = None,
    chat_settings: ChatModel | None = None,
) -> None:
    """Handle successful payment - add credits to user/chat."""
    if not message.successful_payment or not message.from_user:
        return

    payment = message.successful_payment
    payload = payment.invoice_payload
    parts = payload.split(":")

    if len(parts) < 3:
        logfire.error("invalid_payment_payload", payload=payload)
        await message.answer(
            _("Payment received but credits could not be added. Contact support.")
        )
        return

    pack_id = parts[0]
    target_type = parts[1]
    # target_id = parts[2]  # Available for future use if needed

    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        logfire.error("unknown_pack_in_payment", pack_id=pack_id)
        await message.answer(
            _("Payment received but credits could not be added. Contact support.")
        )
        return

    if not user:
        logfire.error("no_user_for_payment", user_id=message.from_user.id)
        await message.answer(
            _("Payment received but your account was not found. Contact support.")
        )
        return

    db = get_db_manager()
    async with db.session() as session:
        service = CreditService(session)

        try:
            if target_type == "chat" and chat_settings:
                new_balance = await service.purchase_credits(
                    user_id=user.id,
                    chat_id=chat_settings.id,
                    amount=pack.credits,
                    telegram_charge_id=payment.telegram_payment_charge_id,
                    pack_name=pack.name,
                )
                await message.answer(
                    _(
                        "✅ **Payment successful!**\n\n"
                        "Added **{credits}** credits to this chat.\n"
                        "New balance: **{balance}** credits"
                    ).format(credits=pack.credits, balance=new_balance),
                    parse_mode="Markdown",
                )
            else:
                new_balance = await service.purchase_credits(
                    user_id=user.id,
                    chat_id=None,
                    amount=pack.credits,
                    telegram_charge_id=payment.telegram_payment_charge_id,
                    pack_name=pack.name,
                )
                await message.answer(
                    _(
                        "✅ **Payment successful!**\n\n"
                        "Added **{credits}** credits to your account.\n"
                        "New balance: **{balance}** credits"
                    ).format(credits=pack.credits, balance=new_balance),
                    parse_mode="Markdown",
                )

            logfire.info(
                "payment_processed",
                pack_id=pack_id,
                credits=pack.credits,
                stars=pack.stars,
                user_id=user.telegram_id,
                target_type=target_type,
                charge_id=payment.telegram_payment_charge_id,
            )

        except Exception:
            logfire.exception(
                "payment_processing_failed",
                charge_id=payment.telegram_payment_charge_id,
            )
            await message.answer(
                _(
                    "Payment received but an error occurred. Contact support with charge ID: {charge_id}"
                ).format(charge_id=payment.telegram_payment_charge_id)
            )
