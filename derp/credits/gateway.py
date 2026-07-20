"""Short-transaction compatibility boundary for the legacy credit service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from derp.credits.service import CreditService
from derp.db.credits import get_balances

if TYPE_CHECKING:
    from derp.credits.types import CreditCheckResult
    from derp.execution import ExecutionPlan
    from derp.history.service import HistoryWindow
    from derp.models import Chat, User

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class CreditServiceGateway:
    """Expose legacy credit operations without leaking session ownership.

    Each method opens and closes its own database unit of work. Callers can
    therefore perform provider or Telegram I/O between operations without
    retaining a database transaction or pooled connection.
    """

    def __init__(self, transactions: TransactionFactory) -> None:
        self._transactions = transactions

    async def get_balances(
        self,
        user_telegram_id: int,
        chat_telegram_id: int | None,
    ) -> tuple[int, int]:
        """Read legacy aggregate balances without exposing a live session."""
        async with self._transactions() as session:
            return await get_balances(
                session,
                user_telegram_id,
                chat_telegram_id,
            )

    async def get_orchestrator_config(
        self,
        user: User,
        chat: Chat,
    ) -> tuple[ExecutionPlan, HistoryWindow]:
        """Resolve the legacy chat plan inside a short transaction."""
        async with self._transactions() as session:
            return await CreditService(session).get_orchestrator_config(user, chat)

    async def check_tool_access(
        self,
        user: User,
        chat: Chat,
        tool_name: str,
        *,
        arguments: Mapping[str, object] | None = None,
    ) -> CreditCheckResult:
        """Resolve legacy feature access inside a short transaction."""
        async with self._transactions() as session:
            return await CreditService(session).check_tool_access(
                user,
                chat,
                tool_name,
                arguments=arguments,
            )

    async def deduct(
        self,
        result: CreditCheckResult,
        user: User,
        chat: Chat,
        tool_name: str,
        *,
        idempotency_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Settle a successful legacy operation in its own transaction."""
        async with self._transactions() as session:
            await CreditService(session).deduct(
                result,
                user,
                chat,
                tool_name,
                idempotency_key=idempotency_key,
                metadata=metadata,
            )

    async def purchase_credits(
        self,
        user: User,
        chat: Chat | None,
        amount: int,
        telegram_charge_id: str,
        *,
        pack_name: str | None = None,
    ) -> int:
        """Fulfil a legacy purchase in its own transaction."""
        async with self._transactions() as session:
            return await CreditService(session).purchase_credits(
                user,
                chat,
                amount,
                telegram_charge_id,
                pack_name=pack_name,
            )

    async def refund_credits(self, telegram_charge_id: str) -> bool:
        """Reverse a legacy purchase in its own transaction."""
        async with self._transactions() as session:
            return await CreditService(session).refund_credits(telegram_charge_id)


__all__ = ["CreditServiceGateway"]
