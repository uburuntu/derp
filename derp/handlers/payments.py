"""Suspended credit-purchase intake and payment reconciliation."""

from __future__ import annotations

import logfire
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _

from derp.common.sender import MessageSender
from derp.credits import CreditService
from derp.credits.packs import CREDIT_PACKS
from derp.credits.purchase_suspension import (
    PurchaseIntakeSource,
    reject_purchase_callback,
    reject_purchase_pre_checkout,
)
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception, telemetry_fingerprint

router = Router(name="payments")
intake_router = Router(name="credit_purchase_intake")
reconciliation_router = Router(name="credit_payment_reconciliation")
router.include_routers(intake_router, reconciliation_router)


@intake_router.callback_query(F.data.startswith("buy:"))
async def handle_buy_callback(
    callback: CallbackQuery,
) -> None:
    """Reject stale credit-pack buttons without creating an invoice."""
    await reject_purchase_callback(callback, PurchaseIntakeSource.CALLBACK)


@intake_router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
    """Reject legacy credit invoices before Telegram captures Stars."""
    await reject_purchase_pre_checkout(
        pre_checkout,
        PurchaseIntakeSource.PRE_CHECKOUT,
    )


@reconciliation_router.message(F.successful_payment)
async def handle_successful_payment(
    message: Message,
    sender: MessageSender,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> None:
    """Handle successful payment - add credits to user/chat."""
    if not message.successful_payment or not message.from_user:
        return

    payment = message.successful_payment
    payload = payment.invoice_payload
    parts = payload.split(":")

    if len(parts) < 3:
        logfire.error("invalid_payment_payload")
        await message.answer(
            _("Payment received but credits could not be added. Contact support.")
        )
        return

    pack_id = parts[0]
    target_type = parts[1]

    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        logfire.error("unknown_pack_in_payment", pack_id=pack_id)
        await message.answer(
            _("Payment received but credits could not be added. Contact support.")
        )
        return

    if not user_model:
        logfire.error("no_user_for_payment", user_id=message.from_user.id)
        await message.answer(
            _("Payment received but your account was not found. Contact support.")
        )
        return

    try:
        if target_type == "chat" and chat_model:
            new_balance = await credit_service.purchase_credits(
                user_model,
                chat_model,
                pack.credits,
                payment.telegram_payment_charge_id,
                pack_name=pack.name,
            )
            await sender.send(
                _(
                    "✅ **Payment successful!**\n\n"
                    "Added **{credits}** credits to this chat.\n"
                    "New balance: **{balance}** credits"
                ).format(credits=pack.credits, balance=new_balance),
            )
        else:
            new_balance = await credit_service.purchase_credits(
                user_model,
                None,
                pack.credits,
                payment.telegram_payment_charge_id,
                pack_name=pack.name,
            )
            await sender.send(
                _(
                    "✅ **Payment successful!**\n\n"
                    "Added **{credits}** credits to your account.\n"
                    "New balance: **{balance}** credits"
                ).format(credits=pack.credits, balance=new_balance),
            )

        logfire.info(
            "payment_processed",
            pack_id=pack_id,
            credits=pack.credits,
            stars=pack.stars,
            user_id=user_model.telegram_id,
            target_type=target_type,
            charge_fingerprint=telemetry_fingerprint(
                payment.telegram_payment_charge_id
            ),
        )

    except Exception:
        report_exception(
            "payment_processing_failed",
            charge_fingerprint=telemetry_fingerprint(
                payment.telegram_payment_charge_id
            ),
        )
        await message.answer(
            _(
                "Payment received but an error occurred. Contact support with charge ID: {charge_id}"
            ).format(charge_id=payment.telegram_payment_charge_id)
        )
