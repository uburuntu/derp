"""Tests for the short-transaction legacy credit compatibility boundary."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from derp.credits.gateway import CreditServiceGateway


class TransactionRecorder:
    """Record transaction lifetimes without emulating SQLAlchemy internals."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.sessions: list[MagicMock] = []
        self.exits = 0

    @asynccontextmanager
    async def transaction(self):
        session = MagicMock(name=f"session_{len(self.sessions)}")
        self.sessions.append(session)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield session
        finally:
            self.active -= 1
            self.exits += 1


@pytest.mark.asyncio
async def test_gateway_delegates_each_operation_in_an_independent_transaction() -> None:
    transactions = TransactionRecorder()
    gateway = CreditServiceGateway(transactions.transaction)
    user = MagicMock(name="user")
    chat = MagicMock(name="chat")
    access = MagicMock(name="access")
    config = MagicMock(name="config")

    service = MagicMock()
    service.get_orchestrator_config = AsyncMock(return_value=config)
    service.check_tool_access = AsyncMock(return_value=access)
    service.deduct = AsyncMock()
    service.purchase_credits = AsyncMock(return_value=150)
    service.refund_credits = AsyncMock(return_value=True)

    with (
        patch(
            "derp.credits.gateway.get_balances",
            new=AsyncMock(return_value=(20, 10)),
        ) as balances,
        patch(
            "derp.credits.gateway.CreditService", return_value=service
        ) as service_factory,
    ):
        assert await gateway.get_balances(123, -456) == (20, 10)
        balances.assert_awaited_once_with(transactions.sessions[0], 123, -456)
        assert transactions.active == 0

        assert await gateway.get_orchestrator_config(user, chat) is config
        assert transactions.active == 0

        assert (
            await gateway.check_tool_access(
                user,
                chat,
                "video_generate",
                arguments={"duration_seconds": 8},
            )
            is access
        )
        assert transactions.active == 0

        await gateway.deduct(
            access,
            user,
            chat,
            "video_generate",
            idempotency_key="operation-1",
            metadata={"source": "tool"},
        )
        assert transactions.active == 0

        assert (
            await gateway.purchase_credits(
                user,
                chat,
                150,
                "charge-1",
                pack_name="large",
            )
            == 150
        )
        assert transactions.active == 0

        assert await gateway.refund_credits("charge-1")

    assert transactions.active == 0
    assert transactions.max_active == 1
    assert transactions.exits == 6
    assert service_factory.call_args_list == [
        call(item) for item in transactions.sessions[1:]
    ]
    service.get_orchestrator_config.assert_awaited_once_with(user, chat)
    service.check_tool_access.assert_awaited_once_with(
        user,
        chat,
        "video_generate",
        arguments={"duration_seconds": 8},
    )
    service.deduct.assert_awaited_once_with(
        access,
        user,
        chat,
        "video_generate",
        idempotency_key="operation-1",
        metadata={"source": "tool"},
    )
    service.purchase_credits.assert_awaited_once_with(
        user,
        chat,
        150,
        "charge-1",
        pack_name="large",
    )
    service.refund_credits.assert_awaited_once_with("charge-1")


@pytest.mark.asyncio
async def test_gateway_closes_transaction_when_legacy_service_raises() -> None:
    transactions = TransactionRecorder()
    gateway = CreditServiceGateway(transactions.transaction)
    service = MagicMock()
    service.refund_credits = AsyncMock(side_effect=RuntimeError("failed"))

    with (
        patch("derp.credits.gateway.CreditService", return_value=service),
        pytest.raises(RuntimeError, match="failed"),
    ):
        await gateway.refund_credits("charge-1")

    assert transactions.active == 0
    assert transactions.exits == 1
