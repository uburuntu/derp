"""Inject durable Stars intent and settlement services."""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from derp.billing import (
    PaymentSettlementService,
    PurchaseIntentService,
    SubscriptionManagementService,
)
from derp.db import DatabaseManager


class CommerceDependency(StrEnum):
    """Commerce boundaries that a matched route may request."""

    PURCHASE_INTENTS = "purchase_intents"
    PAYMENT_SETTLEMENT = "payment_settlement"
    SUBSCRIPTION_MANAGEMENT = "subscription_management"


class CommerceMiddleware(BaseMiddleware):
    """Inject commerce services whose methods own short transactions."""

    def __init__(self, db: DatabaseManager) -> None:
        self._purchase_intents = PurchaseIntentService(db.session)
        self._payment_settlement = PaymentSettlementService(db.session)
        self._subscription_management = SubscriptionManagementService(db.session)

    def inject(
        self,
        data: dict[str, Any],
        dependencies: frozenset[CommerceDependency] | None = None,
    ) -> None:
        """Inject only the commerce boundaries selected for a matched route."""
        selected = (
            frozenset(CommerceDependency) if dependencies is None else dependencies
        )
        if CommerceDependency.PURCHASE_INTENTS in selected:
            data[CommerceDependency.PURCHASE_INTENTS.value] = self._purchase_intents
        if CommerceDependency.PAYMENT_SETTLEMENT in selected:
            data[CommerceDependency.PAYMENT_SETTLEMENT.value] = self._payment_settlement
        if CommerceDependency.SUBSCRIPTION_MANAGEMENT in selected:
            data[CommerceDependency.SUBSCRIPTION_MANAGEMENT.value] = (
                self._subscription_management
            )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        self.inject(data)
        return await handler(event, data)
