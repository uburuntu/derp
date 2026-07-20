"""Inject durable Stars intent and settlement services."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from derp.billing import PaymentSettlementService, PurchaseIntentService
from derp.db import DatabaseManager


class CommerceMiddleware(BaseMiddleware):
    """Inject commerce services whose methods own short transactions."""

    def __init__(self, db: DatabaseManager) -> None:
        self._purchase_intents = PurchaseIntentService(db.session)
        self._payment_settlement = PaymentSettlementService(db.session)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["purchase_intents"] = self._purchase_intents
        data["payment_settlement"] = self._payment_settlement
        return await handler(event, data)
