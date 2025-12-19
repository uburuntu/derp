"""Middleware for injecting MessageSender into handlers.

This middleware creates a MessageSender instance from the incoming message
and injects it into handler data for convenient, boilerplate-free usage.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class MessageSenderMiddleware(BaseMiddleware):
    """Middleware that injects a MessageSender into handler data.

    Extracts the Message from various event types and creates a ready-to-use
    MessageSender instance that handlers can use directly.

    Usage in handlers:
        async def my_handler(
            message: Message,
            sender: MessageSender,
        ):
            await sender.reply("Hello!")  # No need to pass message
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Import here to avoid circular imports
        from derp.common.sender import MessageSender

        message: Message | None = None

        # Extract message from various event types
        if isinstance(event, Message):
            message = event
        elif isinstance(event, CallbackQuery) and event.message:
            # For callback queries, use the message the button was attached to
            if isinstance(event.message, Message):
                message = event.message

        # Inject sender if we have a message
        if message is not None:
            data["sender"] = MessageSender.from_message(message)

        return await handler(event, data)
