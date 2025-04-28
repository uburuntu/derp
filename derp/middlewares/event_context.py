from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import TelegramObject, Update


class EventContextMiddleware(UserContextMiddleware):
    """
    Extracts event's main entities to the handler's parameters
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            raise RuntimeError("Got an unexpected event type")

        data["bot"] = event.bot

        event_context = self.resolve_event_context(event=event)

        if event_context.user is not None:
            data["user"] = event_context.user

        if event_context.chat is not None:
            data["chat"] = event_context.chat

        if event_context.thread_id is not None:
            data["thread_id"] = event_context.thread_id

        if event_context.business_connection_id is not None:
            data["business_connection_id"] = event_context.business_connection_id

        return await handler(event, data)
