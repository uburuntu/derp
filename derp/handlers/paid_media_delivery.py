"""Capability-neutral Telegram recovery for durable paid media delivery."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import gettext as _

from derp.delivery import (
    Delivered,
    DeliveryAuthorizationError,
    DeliveryFailed,
    DeliveryService,
    DeliveryStateError,
    DeliveryUncertain,
    PaidMediaResendCallback,
    ProgressStage,
    ResendCallbackAuthorization,
    ResendResult,
)
from derp.features.paid_media_operation import (
    PaidMediaDelivered,
    PaidMediaDeliveryUncertain,
    PaidMediaInProgress,
    PaidMediaOperationOutcome,
    PaidMediaRefunded,
)
from derp.history.capture import suppress_outbound_history
from derp.observability import report_exception

router = Router(name="paid_media_delivery")


def paid_media_resend_outcome(
    result: ResendResult,
    *,
    resend_token: str,
) -> PaidMediaOperationOutcome:
    """Map server-settled delivery state without opening another charge."""
    outcome = result.outcome
    if isinstance(outcome, Delivered):
        return PaidMediaDelivered(result.operation_id, outcome.message_ids)
    if isinstance(outcome, DeliveryUncertain):
        if outcome.code == "attempt_in_progress_or_interrupted":
            return PaidMediaInProgress(
                result.operation_id,
                ProgressStage.DELIVERING,
                outcome.code,
            )
        return PaidMediaDeliveryUncertain(
            result.operation_id,
            outcome.code,
            resend_token,
        )
    if isinstance(outcome, DeliveryFailed) and outcome.retryable:
        return PaidMediaInProgress(
            result.operation_id,
            ProgressStage.DELIVERING,
            outcome.code,
        )
    if isinstance(outcome, DeliveryFailed):
        return PaidMediaRefunded(result.operation_id, outcome.code)
    raise TypeError("delivery service returned an unsupported resend outcome")


def paid_media_resend_markup(token: str) -> InlineKeyboardMarkup:
    """Build one authenticated, no-charge resend control."""
    callback_data = PaidMediaResendCallback(token=token).pack()
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("paid-media resend callback exceeds 64 bytes")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Send again"),
                    callback_data=callback_data,
                )
            ]
        ]
    )


@router.callback_query(PaidMediaResendCallback.filter())
async def resend_paid_media_delivery(
    callback: CallbackQuery,
    callback_data: PaidMediaResendCallback,
    delivery_service: DeliveryService,
) -> Message | None:
    """Retry only for the persisted requester, chat, and topic capability."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer(
            _("This button is no longer available."),
            show_alert=True,
        )
        return None

    await callback.answer()
    try:
        result = await delivery_service.resend_from_callback(
            ResendCallbackAuthorization(
                token=callback_data.token,
                actor_user_id=callback.from_user.id,
                chat_id=message.chat.id,
                thread_id=message.message_thread_id,
            )
        )
    except DeliveryAuthorizationError:
        return None
    except DeliveryStateError:
        await _edit_control(
            message,
            _("Checking delivery..."),
        )
        return message
    except Exception as exc:
        report_exception(
            "paid_media_resend_failed",
            exception=exc,
            telegram_chat_id=message.chat.id,
            telegram_user_id=callback.from_user.id,
        )
        return None

    outcome = paid_media_resend_outcome(
        result,
        resend_token=callback_data.token,
    )
    if isinstance(outcome, PaidMediaDelivered):
        await _delete_control(message)
        return message
    if isinstance(outcome, PaidMediaDeliveryUncertain):
        await _edit_control(
            message,
            _(
                "This may already be in the chat. You won't be charged again. "
                "Check first, then tap Send again if it's missing."
            ),
            reply_markup=paid_media_resend_markup(outcome.resend_token),
        )
        return message
    if isinstance(outcome, PaidMediaRefunded):
        await _edit_control(
            message,
            _("I couldn't deliver it. Your credits were returned."),
        )
        return message
    await _edit_control(message, _("Sending it now..."))
    return message


@router.callback_query(F.data.startswith("pmr:"))
async def reject_malformed_paid_media_resend(callback: CallbackQuery) -> None:
    """Reject malformed recovery controls without touching durable state."""
    await callback.answer(
        _("This button is no longer available."),
        show_alert=True,
    )


async def _delete_control(message: Message) -> None:
    try:
        with suppress_outbound_history():
            await message.delete()
    except Exception as exc:
        report_exception(
            "paid_media_delivery_control_delete_failed",
            exception=exc,
            level="warning",
        )


async def _edit_control(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        with suppress_outbound_history():
            await message.edit_text(text, reply_markup=reply_markup)
    except Exception as exc:
        report_exception(
            "paid_media_delivery_control_edit_failed",
            exception=exc,
            level="warning",
        )


__all__ = [
    "paid_media_resend_markup",
    "paid_media_resend_outcome",
    "reject_malformed_paid_media_resend",
    "resend_paid_media_delivery",
    "router",
]
