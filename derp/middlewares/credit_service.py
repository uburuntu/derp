"""Middleware for injecting CreditService into handlers.

This middleware opens a transactional session and provides a ready-to-use
CreditService instance to handlers that need credit operations.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import logfire
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from derp.credits import CreditService
from derp.db import DatabaseManager


class CreditServiceMiddleware(BaseMiddleware):
    """Middleware that injects a CreditService into handler data.

    Opens a transactional session for the duration of the handler execution.
    Commits on success, rolls back on exception.

    Usage in handlers:
        async def my_handler(
            message: Message,
            credit_service: CreditService,
        ):
            result = await credit_service.check_tool_access(...)
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            async with self.db.session() as session:
                data["credit_service"] = CreditService(session)
                return await handler(event, data)
        except Exception:
            logfire.exception("credit_service_middleware_error")
            raise
