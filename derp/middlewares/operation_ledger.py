"""Inject the short-transaction paid-operation boundary."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from derp.db import DatabaseManager
from derp.operations import OperationLedger


class OperationLedgerMiddleware(BaseMiddleware):
    """Inject a ledger whose individual methods own their transactions."""

    def __init__(self, db: DatabaseManager) -> None:
        self._ledger = OperationLedger(db.session)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["operation_ledger"] = self._ledger
        return await handler(event, data)
