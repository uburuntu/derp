"""Privacy boundary for actor-only Telegram control-plane replies."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, Message

from derp.common.sender import MessageSender
from derp.history.capture import suppress_outbound_history
from derp.observability import report_exception


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
    if message.chat.type == "private":
        with suppress_outbound_history():
            return await sender.reply(text, reply_markup=reply_markup)

    if recipient_chat_id is None:
        return await sender.reply(public_failure)

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
        return await sender.reply(public_failure)
    return await sender.reply(public_success)


__all__ = ["deliver_sensitive_reply"]
