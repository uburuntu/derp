"""Dispatcher-level middleware shared by every update."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from derp.ops.observability import update_span


class UpdateSpanMiddleware(BaseMiddleware):
    """One ``telegram.update`` span per update, numeric attributes only."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        with update_span(event):
            return await handler(event, data)
