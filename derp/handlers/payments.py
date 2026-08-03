"""Durable Telegram Stars purchase adapters."""

from __future__ import annotations

import uuid
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
from aiogram.utils.i18n import gettext as _

from derp.billing import (
    CLOSED_COMMERCE_POLICY,
    ActiveSubscriptionError,
    CapturedPayment,
    CommercePolicy,
    FulfillmentState,
    PaymentConflictError,
    PaymentReplyDisposition,
    PaymentSettlementService,
    PaymentSettlementState,
    PaymentUpdateDisposition,
    PaymentUpdateInboxService,
    PaymentUpdateOutcome,
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
    PurchaseTermsAcceptCallback,
    create_stars_invoice_link,
)
from derp.common.private_delivery import (
    SensitiveReplyDisposition,
    SensitiveReplyResult,
    deliver_sensitive_reply_with_disposition,
)
from derp.common.sender import MessageSender
from derp.handlers.legal_support import (
    TermsAcceptanceSource,
    build_terms_panel,
    terms_callback_version,
)
from derp.legal import TERMS_ACCEPTANCE_VERSION, TermsAcceptanceRequiredError
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception, telemetry_fingerprint
from derp.operator import OperatorAccessPolicy
from derp.support import SupportRequestService, TermsAcceptanceService

router = Router(name="payments")
intake_router = Router(name="credit_purchase_intake")
reconciliation_router = Router(name="credit_payment_reconciliation")
router.include_routers(intake_router, reconciliation_router)


def _star_amount(stars: int) -> str:
    return _("{stars} Star", "{stars} Stars", stars).format(stars=stars)


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
            _("Credit purchases aren't available yet. You won't be charged."),
            show_alert=True,
        )
    if not isinstance(callback.message, Message) or not user_model:
        return await callback.answer(
            _(
                "This purchase is unavailable. You won't be charged. "
                "Open /buy and try again."
            ),
            show_alert=True,
        )
    if user_model.telegram_id != callback.from_user.id:
        return await callback.answer(
            _(
                "This purchase is no longer valid. You won't be charged. "
                "Open /buy again."
            ),
            show_alert=True,
        )
    if callback_data.actor_id not in {0, callback.from_user.id}:
        return await callback.answer(
            _(
                "This purchase menu belongs to someone else. You won't be charged. "
                "Open /buy for your own menu."
            ),
            show_alert=True,
        )

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
    except TermsAcceptanceRequiredError:
        text, markup = build_terms_panel(
            accepted=False,
            source=TermsAcceptanceSource.PURCHASE_GATE,
            actor_telegram_id=callback.from_user.id,
            pending_purchase=callback_data,
        )
        await callback.message.answer(text, reply_markup=markup, protect_content=True)
        return await callback.answer(
            _("Review and accept the current terms before buying."),
            show_alert=True,
        )
    except ActiveSubscriptionError:
        return await callback.answer(
            _("Your monthly plan is already active"),
            show_alert=True,
        )
    except LookupError, UnknownProductError, ValueError:
        return await callback.answer(
            _(
                "This purchase option is no longer available. You won't be charged. "
                "Open /buy again."
            ),
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
            _(
                "Telegram couldn't prepare the invoice. You won't be charged. "
                "Try again."
            ),
            show_alert=True,
        )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Pay {price}").format(price=_star_amount(handle.stars)),
                    url=invoice_link,
                )
            ]
        ]
    )
    owner = _("this chat") if handle.target.kind.value == "chat" else _("you")
    await callback.message.answer(
        _(
            "<b>{credits} credit for {owner}</b>\n"
            "Telegram will ask you to confirm before charging {price}.",
            "<b>{credits} credits for {owner}</b>\n"
            "Telegram will ask you to confirm before charging {price}.",
            handle.credits,
        ).format(
            credits=handle.credits,
            owner=owner,
            price=_star_amount(handle.stars),
        ),
        reply_markup=markup,
    )
    await callback.answer(_("Invoice ready"))
    logfire.info(
        "purchase_intent_presented",
        intent_id=str(handle.intent_id),
        product_id=handle.product_id,
        product_version=handle.product_version,
        target=handle.target.kind.value,
        user_id=user_model.telegram_id,
    )


@intake_router.callback_query(PurchaseTermsAcceptCallback.filter())
async def accept_purchase_terms(
    callback: CallbackQuery,
    callback_data: PurchaseTermsAcceptCallback,
    purchase_intents: PurchaseIntentService,
    terms_acceptance: TermsAcceptanceService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
) -> None:
    """Accept current Terms and immediately resume the exact selected purchase."""
    if (
        not isinstance(callback.message, Message)
        or user_model is None
        or user_model.telegram_id != callback.from_user.id
        or callback_data.actor_id != callback.from_user.id
    ):
        await callback.answer(
            _(
                "This purchase is no longer valid. You won't be charged. Open /buy again."
            ),
            show_alert=True,
        )
        return
    if callback_data.version != terms_callback_version():
        await callback.answer(
            _("These terms changed. Open /buy and review the current version."),
            show_alert=True,
        )
        return
    await terms_acceptance.accept_current(
        user_model.id,
        source=TermsAcceptanceSource.PURCHASE_GATE.value,
    )
    await callback.message.edit_text(_("Terms accepted. Continuing your purchase…"))
    logfire.info(
        "legal.terms_accepted",
        terms_version=TERMS_ACCEPTANCE_VERSION,
        user_id=user_model.telegram_id,
        purchase_continuation=True,
    )
    await handle_buy_callback(
        callback,
        callback_data.purchase(),
        purchase_intents,
        user_model,
        chat_model,
        commerce_policy,
    )


@intake_router.callback_query(F.data.startswith("buy:"))
async def reject_malformed_buy_callback(callback: CallbackQuery) -> None:
    """Fail closed for stale selectors that do not match the typed callback."""
    await callback.answer(
        _("This purchase option expired. You weren't charged. Open /buy again."),
        show_alert=True,
    )


@intake_router.pre_checkout_query()
async def handle_pre_checkout(
    pre_checkout: PreCheckoutQuery,
    purchase_intents: PurchaseIntentService,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
    operator_access: OperatorAccessPolicy | None = None,
) -> None:
    """Approve only an exact, unexpired, server-stored purchase intent."""
    decision = await purchase_intents.validate_pre_checkout(
        PreCheckoutRequest(
            invoice_payload=pre_checkout.invoice_payload,
            payer_telegram_id=pre_checkout.from_user.id,
            currency=pre_checkout.currency,
            total_amount=pre_checkout.total_amount,
        ),
        public_intake_enabled=commerce_policy.public_intake_enabled,
        operator_debug_allowed=(
            operator_access is not None
            and operator_access.allows(pre_checkout.from_user.id)
        ),
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
        error_message=_(
            "This invoice expired or changed. You won't be charged. "
            "Open /buy and try again."
        ),
    )


@reconciliation_router.message(F.successful_payment)
async def handle_successful_payment(
    message: Message,
    sender: MessageSender,
    payment_settlement: PaymentSettlementService,
    payment_update_inbox: PaymentUpdateInboxService | None = None,
    payment_update_inbox_id: uuid.UUID | None = None,
) -> None:
    """Record every captured charge, then fulfill matching terms exactly once."""
    payment = message.successful_payment
    if not payment or not message.from_user:
        return

    inbox_outcome: PaymentUpdateOutcome | None = None
    try:
        if payment_update_inbox is not None and payment_update_inbox_id is not None:
            inbox_outcome = await payment_update_inbox.reconcile(
                payment_update_inbox_id
            )
            if inbox_outcome.disposition is PaymentUpdateDisposition.RETRY_SCHEDULED:
                await _deliver_payment_reply(
                    message,
                    sender,
                    _(
                        "Payment recorded. Credits are still processing. "
                        "Don't pay again; check /credits shortly."
                    ),
                )
                return
            if inbox_outcome.disposition in {
                PaymentUpdateDisposition.BUSY,
                PaymentUpdateDisposition.TERMINAL,
            }:
                return
            result = inbox_outcome.fulfillment
        else:
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
        await _deliver_payment_reply(
            message,
            sender,
            _(
                "Telegram charged this payment, but your credits haven't been "
                "added. The payment needs review. Don't buy again. Open /support."
            ),
        )
        return

    if inbox_outcome is not None and (
        inbox_outcome.disposition is PaymentUpdateDisposition.ATTENTION
    ):
        text = _(
            "Telegram charged this payment, but no credits were added because the "
            "details need review. Don't pay again. Open /support."
        )
    elif (result is not None and result.state is FulfillmentState.CLAWED_BACK) or (
        inbox_outcome is not None
        and inbox_outcome.settlement_state is PaymentSettlementState.CLAWED_BACK
    ):
        text = _(
            "This payment was already refunded. No credits were added. Check /credits."
        )
    elif result is None:
        text = _("Payment processed. Check /credits.")
    elif result.state is FulfillmentState.NEEDS_REVIEW:
        text = _(
            "Telegram charged this payment, but no credits were added because the "
            "details need review. Don't pay again. Open /support."
        )
    elif result.idempotent:
        text = _("This payment was already applied. No credits changed this time.")
    elif result.subscription_cycle_id is not None:
        text = _(
            "<b>Plan active</b>\n{credits} monthly credit is available.",
            "<b>Plan active</b>\n{credits} monthly credits are available.",
            result.available_credits,
        ).format(credits=result.available_credits)
    else:
        lines = [
            _("<b>Payment complete</b>"),
            _(
                "Purchased credit available: {credits}",
                "Purchased credits available: {credits}",
                result.available_credits,
            ).format(credits=result.available_credits),
        ]
        if result.debt_offset_credits:
            lines.append(
                _(
                    "Used to repay payment debt: {credits} credit",
                    "Used to repay payment debt: {credits} credits",
                    result.debt_offset_credits,
                ).format(credits=result.debt_offset_credits)
            )
        text = "\n".join(lines)
    delivery = await _deliver_payment_reply(message, sender, text)
    if inbox_outcome is not None and inbox_outcome.needs_notification:
        await payment_update_inbox.finish_notification(
            inbox_outcome,
            _payment_reply_disposition(delivery),
        )

    if result is not None:
        logfire.info(
            "payment_reconciled",
            receipt_id=str(result.receipt_id),
            state=result.state.value,
            idempotent=result.idempotent,
            charge_fingerprint=telemetry_fingerprint(
                payment.telegram_payment_charge_id
            ),
        )


@reconciliation_router.message(F.refunded_payment)
async def handle_refunded_payment(
    message: Message,
    sender: MessageSender,
    payment_settlement: PaymentSettlementService,
    payment_update_inbox: PaymentUpdateInboxService | None = None,
    payment_update_inbox_id: uuid.UUID | None = None,
    support_requests: SupportRequestService | None = None,
) -> None:
    """Validate and reconcile each Telegram refund against its captured charge."""
    payment = message.refunded_payment
    if not payment:
        return

    charge_fingerprint = telemetry_fingerprint(payment.telegram_payment_charge_id)
    inbox_outcome: PaymentUpdateOutcome | None = None
    try:
        if payment_update_inbox is not None and payment_update_inbox_id is not None:
            inbox_outcome = await payment_update_inbox.reconcile(
                payment_update_inbox_id
            )
            if inbox_outcome.disposition is PaymentUpdateDisposition.RETRY_SCHEDULED:
                await _deliver_refund_reply(
                    message,
                    sender,
                    _(
                        "Refund recorded. Balance changes are still processing. "
                        "Check /credits shortly."
                    ),
                )
                return
            if inbox_outcome.disposition in {
                PaymentUpdateDisposition.BUSY,
                PaymentUpdateDisposition.TERMINAL,
            }:
                return
            result = inbox_outcome.clawback
        else:
            result = await payment_settlement.clawback(_refunded_payment(payment))
    except (LookupError, PaymentConflictError, ValueError) as exc:
        logfire.warning(
            "payment_refund_needs_review",
            error_type=type(exc).__name__,
            charge_fingerprint=charge_fingerprint,
        )
        await _deliver_refund_reply(
            message,
            sender,
            _(
                "Telegram sent a refund, but it needs review. No credits changed. "
                "Open /support."
            ),
        )
        return
    except Exception as exc:
        report_exception(
            "payment_refund_reconciliation_failed",
            exception=exc,
            charge_fingerprint=charge_fingerprint,
        )
        await _deliver_refund_reply(
            message,
            sender,
            _(
                "Telegram sent a refund, but it needs review. No credits changed. "
                "Open /support."
            ),
        )
        return

    if support_requests is not None:
        try:
            if result is not None:
                await support_requests.complete_refund(result.receipt_id)
            elif (
                inbox_outcome is not None
                and inbox_outcome.settlement_state is PaymentSettlementState.REFUNDED
            ):
                await support_requests.complete_reconciled_refunds(limit=10)
        except Exception as exc:
            report_exception(
                "support_refund_completion_failed",
                exception=exc,
                level="warning",
                charge_fingerprint=charge_fingerprint,
            )

    if inbox_outcome is not None and (
        inbox_outcome.disposition is PaymentUpdateDisposition.ATTENTION
    ):
        text = _(
            "Telegram sent a refund, but it needs review. No credits changed. "
            "Open /support."
        )
    elif result is None:
        text = _("Refund processed. Check /credits.")
    elif result.idempotent:
        text = _("This refund was already applied. No credits changed this time.")
    else:
        lines = [
            _("<b>Refund complete</b>"),
            _(
                "Unused credit removed: {credits}",
                "Unused credits removed: {credits}",
                result.removed_available_credits,
            ).format(credits=result.removed_available_credits),
        ]
        if result.debt_created_credits:
            lines.append(
                _(
                    "Payment debt added: {credits} credit. Paid features are paused.",
                    "Payment debt added: {credits} credits. Paid features are paused.",
                    result.debt_created_credits,
                ).format(credits=result.debt_created_credits)
            )
        text = "\n".join(lines)
    delivery = await _deliver_refund_reply(message, sender, text)
    if inbox_outcome is not None and inbox_outcome.needs_notification:
        await payment_update_inbox.finish_notification(
            inbox_outcome,
            _payment_reply_disposition(delivery),
        )

    if result is not None:
        logfire.info(
            "payment_refund_reconciled",
            receipt_id=str(result.receipt_id),
            idempotent=result.idempotent,
            removed_credits=result.removed_available_credits,
            debt_credits=result.debt_created_credits,
            charge_fingerprint=charge_fingerprint,
        )


async def _deliver_payment_reply(
    message: Message,
    sender: MessageSender,
    text: str,
) -> SensitiveReplyResult:
    return await deliver_sensitive_reply_with_disposition(
        message,
        sender,
        text,
        recipient_chat_id=message.from_user and message.from_user.id,
        public_success=_("I sent the payment details in a private chat."),
        public_failure=_(
            "I couldn't send the payment details. Open Derp privately and check "
            "/credits."
        ),
        failure_event="private_payment_delivery_failed",
    )


async def _deliver_refund_reply(
    message: Message,
    sender: MessageSender,
    text: str,
) -> SensitiveReplyResult:
    return await deliver_sensitive_reply_with_disposition(
        message,
        sender,
        text,
        recipient_chat_id=message.from_user and message.from_user.id,
        public_success=_("I sent the refund details in a private chat."),
        public_failure=_(
            "I couldn't send the refund details. Open Derp privately and check "
            "/credits."
        ),
        failure_event="private_refund_delivery_failed",
    )


def _payment_reply_disposition(
    delivery: SensitiveReplyResult,
) -> PaymentReplyDisposition:
    return {
        SensitiveReplyDisposition.SENT: PaymentReplyDisposition.SENT,
        SensitiveReplyDisposition.FAILED: PaymentReplyDisposition.FAILED,
        SensitiveReplyDisposition.SKIPPED: PaymentReplyDisposition.SKIPPED,
    }[delivery.disposition]


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
