"""Privacy boundary for actor-only Telegram control-plane replies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiogram.types import InlineKeyboardMarkup, Message

from derp.common.sender import MessageSender
from derp.history.capture import suppress_outbound_history
from derp.observability import report_exception


class SensitiveReplyDisposition(StrEnum):
    """Whether the actor-only part of a sensitive reply reached its target."""

    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SensitiveReplyResult:
    """Telegram response plus the actor-only delivery disposition."""

    message: Message
    disposition: SensitiveReplyDisposition


async def deliver_sensitive_reply(
    message: Message,
    sender: MessageSender,
    text: str,
    *,
    recipient_chat_id: int | None,
    public_success: str,
    public_failure: str,
    failure_event: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Keep sensitive control-plane text private while acknowledging groups."""
    result = await deliver_sensitive_reply_with_disposition(
        message,
        sender,
        text,
        recipient_chat_id=recipient_chat_id,
        public_success=public_success,
        public_failure=public_failure,
        failure_event=failure_event,
        reply_markup=reply_markup,
    )
    return result.message


async def deliver_sensitive_reply_with_disposition(
    message: Message,
    sender: MessageSender,
    text: str,
    *,
    recipient_chat_id: int | None,
    public_success: str,
    public_failure: str,
    failure_event: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> SensitiveReplyResult:
    """Deliver sensitive text while exposing the private delivery result."""
    if message.chat.type == "private":
        with suppress_outbound_history():
            delivered = await sender.reply(text, reply_markup=reply_markup)
        return SensitiveReplyResult(delivered, SensitiveReplyDisposition.SENT)

    if recipient_chat_id is None:
        acknowledgement = await sender.reply(public_failure)
        return SensitiveReplyResult(
            acknowledgement,
            SensitiveReplyDisposition.SKIPPED,
        )

    private_sender = MessageSender(
        bot=message.bot,
        chat_id=recipient_chat_id,
        protect_content=True,
    )
    try:
        with suppress_outbound_history():
            await private_sender.send(text, reply_markup=reply_markup)
    except Exception as exc:
        report_exception(
            failure_event,
            exception=exc,
            level="warning",
            telegram_user_id=recipient_chat_id,
            telegram_chat_id=message.chat.id,
        )
        acknowledgement = await sender.reply(public_failure)
        return SensitiveReplyResult(
            acknowledgement,
            SensitiveReplyDisposition.FAILED,
        )
    acknowledgement = await sender.reply(public_success)
    return SensitiveReplyResult(acknowledgement, SensitiveReplyDisposition.SENT)


__all__ = [
    "SensitiveReplyDisposition",
    "SensitiveReplyResult",
    "deliver_sensitive_reply",
    "deliver_sensitive_reply_with_disposition",
]
