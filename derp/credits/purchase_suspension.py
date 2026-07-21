"""Fail-closed boundary for new credit purchases during the billing rebuild."""

from __future__ import annotations

from enum import StrEnum

import logfire
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _

from derp.common.sender import MessageSender

_SUSPENSION_REASON = "durable_purchase_intents_pending"


class PurchaseIntakeSource(StrEnum):
    """Auditable entry points that can attempt to start a Stars purchase."""

    PERSONAL_COMMAND = "personal_command"
    CHAT_COMMAND = "chat_command"
    CALLBACK = "callback"
    PRE_CHECKOUT = "pre_checkout"
    DEBUG_COMMAND = "debug_command"
    DEBUG_CALLBACK = "debug_callback"
    DEBUG_PRE_CHECKOUT = "debug_pre_checkout"


def purchase_suspension_message() -> str:
    """Return the single user-facing explanation for rejected purchases."""
    return _(
        "Credit purchases are temporarily unavailable. You won't be charged. "
        "Your existing credits and free features still work."
    )


async def reject_purchase_command(
    message: Message,
    sender: MessageSender,
    source: PurchaseIntakeSource,
) -> Message:
    """Reject a purchase command without exposing invoice controls."""
    _record_rejection(source, message.from_user and message.from_user.id)
    return await sender.reply(purchase_suspension_message())


async def reject_purchase_callback(
    callback: CallbackQuery,
    source: PurchaseIntakeSource,
) -> None:
    """Reject stale purchase buttons without creating an invoice."""
    _record_rejection(source, callback.from_user.id)
    await callback.answer(purchase_suspension_message(), show_alert=True)


async def reject_purchase_pre_checkout(
    query: PreCheckoutQuery,
    source: PurchaseIntakeSource,
) -> None:
    """Reject issued invoice links before Telegram captures any Stars."""
    _record_rejection(source, query.from_user.id)
    await query.answer(ok=False, error_message=purchase_suspension_message())


def _record_rejection(
    source: PurchaseIntakeSource,
    user_id: int | None,
) -> None:
    logfire.info(
        "credit_purchase_intake_rejected",
        source=source.value,
        reason=_SUSPENSION_REASON,
        user_id=user_id,
    )
