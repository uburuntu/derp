"""Durable Telegram Stars purchase adapters."""

from __future__ import annotations

from datetime import UTC, datetime

import logfire
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
)
from aiogram.types import (
    RefundedPayment as TelegramRefundedPayment,
)

from derp.billing import (
    CLOSED_COMMERCE_POLICY,
    ActiveSubscriptionError,
    CapturedPayment,
    CommercePolicy,
    FulfillmentState,
    PaymentConflictError,
    PaymentSettlementService,
    PreCheckoutRequest,
    ProductKind,
    PurchaseIntentService,
    PurchaseTarget,
    RefundedPaymentCommand,
    UnknownProductError,
)
from derp.billing.telegram import (
    PurchaseCallback,
    PurchaseTargetCode,
    create_stars_invoice_link,
)
from derp.common.sender import MessageSender
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception, telemetry_fingerprint

router = Router(name="payments")
intake_router = Router(name="credit_purchase_intake")
reconciliation_router = Router(name="credit_payment_reconciliation")
router.include_routers(intake_router, reconciliation_router)


@intake_router.callback_query(PurchaseCallback.filter())
async def handle_buy_callback(
    callback: CallbackQuery,
    callback_data: PurchaseCallback,
    purchase_intents: PurchaseIntentService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
) -> None:
    """Create one server-bound intent, then expose its Telegram invoice link."""
    if not commerce_policy.public_intake_enabled:
        return await callback.answer(
            "Purchases are not enabled yet",
            show_alert=True,
        )
    if not isinstance(callback.message, Message) or not user_model:
        return await callback.answer("Purchase is unavailable", show_alert=True)
    if user_model.telegram_id != callback.from_user.id:
        return await callback.answer("Purchase identity changed", show_alert=True)

    try:
        if callback_data.kind is ProductKind.SUBSCRIPTION:
            if callback_data.target is not PurchaseTargetCode.USER:
                raise ValueError("Subscriptions are personal")
            handle = await purchase_intents.create_subscription_intent(
                payer_user_id=user_model.id
            )
            if handle.product_id != callback_data.product_id:
                raise UnknownProductError("Unknown subscription")
        else:
            if callback_data.target is PurchaseTargetCode.CHAT:
                if not chat_model or chat_model.type == "private":
                    raise ValueError("Chat purchase target is unavailable")
                target = PurchaseTarget.chat(chat_model.id)
            else:
                target = PurchaseTarget.user(user_model.id)
            handle = await purchase_intents.create_top_up_intent(
                payer_user_id=user_model.id,
                target=target,
                product_id=callback_data.product_id,
            )
        invoice_link = await create_stars_invoice_link(
            callback.bot,
            handle,
            business_connection_id=callback.message.business_connection_id,
        )
    except ActiveSubscriptionError:
        return await callback.answer(
            "Your current plan is already active",
            show_alert=True,
        )
    except LookupError, UnknownProductError, ValueError:
        return await callback.answer(
            "This purchase option is no longer available",
            show_alert=True,
        )
    except TelegramAPIError:
        report_exception(
            "purchase_invoice_link_failed",
            level="warning",
            product_id=callback_data.product_id,
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            "Telegram could not prepare the invoice. Try again.",
            show_alert=True,
        )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Pay {handle.stars} Stars",
                    url=invoice_link,
                )
            ]
        ]
    )
    owner = "this chat" if handle.target.kind.value == "chat" else "your wallet"
    await callback.message.answer(
        f"<b>{handle.credits} credits for {owner}</b>\n"
        "Telegram shows the final confirmation before charging.",
        reply_markup=markup,
    )
    await callback.answer("Invoice ready")
    logfire.info(
        "purchase_intent_presented",
        intent_id=str(handle.intent_id),
        product_id=handle.product_id,
        product_version=handle.product_version,
        target=handle.target.kind.value,
        user_id=user_model.telegram_id,
    )


@intake_router.callback_query(F.data.startswith("buy:"))
async def reject_malformed_buy_callback(callback: CallbackQuery) -> None:
    """Fail closed for stale selectors that do not match the typed callback."""
    await callback.answer(
        "This purchase option expired. Open /buy again.",
        show_alert=True,
    )


@intake_router.pre_checkout_query()
async def handle_pre_checkout(
    pre_checkout: PreCheckoutQuery,
    purchase_intents: PurchaseIntentService,
) -> None:
    """Approve only an exact, unexpired, server-stored purchase intent."""
    decision = await purchase_intents.validate_pre_checkout(
        PreCheckoutRequest(
            invoice_payload=pre_checkout.invoice_payload,
            payer_telegram_id=pre_checkout.from_user.id,
            currency=pre_checkout.currency,
            total_amount=pre_checkout.total_amount,
        )
    )
    if decision.approved:
        await pre_checkout.answer(ok=True)
        return
    logfire.info(
        "pre_checkout_rejected",
        rejection=decision.rejection and decision.rejection.value,
        user_id=pre_checkout.from_user.id,
    )
    await pre_checkout.answer(
        ok=False,
        error_message="This invoice expired or changed. Open /buy and try again.",
    )


@reconciliation_router.message(F.successful_payment)
async def handle_successful_payment(
    message: Message,
    sender: MessageSender,
    payment_settlement: PaymentSettlementService,
) -> None:
    """Record every captured charge, then fulfill matching terms exactly once."""
    payment = message.successful_payment
    if not payment or not message.from_user:
        return

    try:
        result = await payment_settlement.fulfill(
            _captured_payment(payment, message.from_user.id)
        )
    except Exception:
        report_exception(
            "payment_fulfillment_failed",
            charge_fingerprint=telemetry_fingerprint(
                payment.telegram_payment_charge_id
            ),
        )
        await sender.send(
            "Telegram charged the payment, but local fulfillment needs review. "
            "You do not need to buy again; support can reconcile this charge."
        )
        return

    if result.state is FulfillmentState.NEEDS_REVIEW:
        await sender.send(
            "Payment received. Its details need review before credits can be "
            "released. You do not need to buy again."
        )
    elif result.idempotent:
        await sender.send("This payment was already applied to its wallet.")
    elif result.subscription_cycle_id is not None:
        await sender.send(
            "<b>Plan active</b>\n"
            f"Monthly allowance available: {result.available_credits} credits."
        )
    else:
        lines = [
            "<b>Payment complete</b>",
            f"Purchased credits available: {result.available_credits}",
        ]
        if result.debt_offset_credits:
            lines.append(f"Applied to prior payment debt: {result.debt_offset_credits}")
        await sender.send("\n".join(lines))

    logfire.info(
        "payment_reconciled",
        receipt_id=str(result.receipt_id),
        state=result.state.value,
        idempotent=result.idempotent,
        charge_fingerprint=telemetry_fingerprint(payment.telegram_payment_charge_id),
    )


@reconciliation_router.message(F.refunded_payment)
async def handle_refunded_payment(
    message: Message,
    sender: MessageSender,
    payment_settlement: PaymentSettlementService,
) -> None:
    """Validate and reconcile each Telegram refund against its captured charge."""
    payment = message.refunded_payment
    if not payment:
        return

    charge_fingerprint = telemetry_fingerprint(payment.telegram_payment_charge_id)
    try:
        result = await payment_settlement.clawback(_refunded_payment(payment))
    except (LookupError, PaymentConflictError, ValueError) as exc:
        logfire.warning(
            "payment_refund_needs_review",
            error_type=type(exc).__name__,
            charge_fingerprint=charge_fingerprint,
        )
        await sender.send(
            "Refund received, but its details need review. No credits were "
            "changed; support can reconcile it safely."
        )
        return
    except Exception as exc:
        report_exception(
            "payment_refund_reconciliation_failed",
            exception=exc,
            charge_fingerprint=charge_fingerprint,
        )
        await sender.send(
            "Refund received, but local reconciliation needs review. No credits "
            "were changed; support can reconcile it safely."
        )
        return

    if result.idempotent:
        await sender.send("This refund was already reconciled.")
    else:
        lines = [
            "<b>Refund reconciled</b>",
            f"Unused credits removed: {result.removed_available_credits}",
        ]
        if result.debt_created_credits:
            lines.append(
                "Credits already used or reserved: "
                f"{result.debt_created_credits} (recorded as payment debt)."
            )
        await sender.send("\n".join(lines))

    logfire.info(
        "payment_refund_reconciled",
        receipt_id=str(result.receipt_id),
        idempotent=result.idempotent,
        removed_credits=result.removed_available_credits,
        debt_credits=result.debt_created_credits,
        charge_fingerprint=charge_fingerprint,
    )


def _captured_payment(
    payment: SuccessfulPayment,
    payer_telegram_id: int,
) -> CapturedPayment:
    """Map allowlisted Telegram fields into the provider-neutral command."""
    expiration = (
        datetime.fromtimestamp(payment.subscription_expiration_date, UTC)
        if payment.subscription_expiration_date is not None
        else None
    )
    return CapturedPayment(
        invoice_payload=payment.invoice_payload,
        telegram_charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id,
        payer_telegram_id=payer_telegram_id,
        currency=payment.currency,
        total_amount=payment.total_amount,
        is_recurring=bool(payment.is_recurring),
        is_first_recurring=bool(payment.is_first_recurring),
        subscription_expiration_at=expiration,
    )


def _refunded_payment(payment: TelegramRefundedPayment) -> RefundedPaymentCommand:
    """Map allowlisted Telegram fields into the provider-neutral command."""
    return RefundedPaymentCommand(
        invoice_payload=payment.invoice_payload,
        telegram_charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id,
        currency=payment.currency,
        total_amount=payment.total_amount,
    )
