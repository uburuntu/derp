"""Inject the short-transaction legacy credit compatibility boundary."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from derp.credits.gateway import CreditServiceGateway
from derp.db import DatabaseManager


class CreditServiceMiddleware(BaseMiddleware):
    """Inject a gateway whose individual methods own their transactions."""

    def __init__(self, db: DatabaseManager) -> None:
        self._gateway = CreditServiceGateway(db.session)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["credit_service"] = self._gateway
        return await handler(event, data)
