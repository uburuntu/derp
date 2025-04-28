from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import Message


class ThrottleUsersMiddleware(UserContextMiddleware):
    """
    Skips user's message if there's ongoing handler for them
    """

    def __init__(self):
        self.active_users = set()

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user

        if user.id in self.active_users:
            return UNHANDLED

        self.active_users.add(user.id)

        try:
            return await handler(event, data)
        finally:
            self.active_users.discard(user.id)
