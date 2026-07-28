"""Database contracts for the bounded operator debug-purchase refund."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.billing import DEFAULT_PRODUCT_CATALOG, PaymentSettlementService
from derp.models import (
    PaymentReceipt,
    PaymentRefundRequest,
    PurchaseIntent,
    User,
    Wallet,
    WalletLot,
)
from derp.operator import OperatorDebugRefundResult, OperatorDebugRefundService

pytestmark = pytest.mark.database

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class DebugPurchase:
    operator_id: int
    receipt_id: UUID
    wallet_lot_id: UUID
    charge_id: str


class FailOnceSettlement:
    """Expose one local crash boundary before delegating idempotently."""

    def __init__(self, delegate: PaymentSettlementService) -> None:
        self._delegate = delegate
        self.calls = 0

    async def clawback(self, refund: str) -> object:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated local settlement failure")
        return await self._delegate.clawback(refund)


def _telegram_id() -> int:
    return 30_000_000 + uuid4().int % 8_000_000_000


def _transactions(db_engine: AsyncEngine) -> TransactionFactory:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    return transactions


def _bot(*, outcome: bool | BaseException = True) -> MagicMock:
    bot = MagicMock(spec=Bot)
    if isinstance(outcome, BaseException):
        bot.refund_star_payment = AsyncMock(side_effect=outcome)
    else:
        bot.refund_star_payment = AsyncMock(return_value=outcome)
    return bot


async def _create_fulfilled_debug_purchase(
    transactions: TransactionFactory,
    *,
    operator_id: int | None = None,
) -> DebugPurchase:
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up
    operator_id = operator_id or _telegram_id()
    charge_id = f"debug-{uuid4().hex}"
    now = datetime.now(UTC)
    async with transactions() as session:
        user = User(
            telegram_id=operator_id,
            is_bot=False,
            first_name="Operator",
        )
        session.add(user)
        await session.flush()
        intent = PurchaseIntent(
            token_hash=uuid4().hex + uuid4().hex,
            payer_user_id=user.id,
            target_user_id=user.id,
            product_kind=product.kind.value,
            product_id=product.id,
            product_version=product.version,
            credits=product.credits,
            stars=product.stars,
            currency=product.currency,
            status="fulfilled",
            expires_at=now + timedelta(minutes=10),
            fulfilled_at=now,
        )
        session.add(intent)
        await session.flush()
        receipt = PaymentReceipt(
            purchase_intent_id=intent.id,
            telegram_charge_id=charge_id,
            provider_charge_id=f"provider-{uuid4().hex}",
            payer_telegram_id=operator_id,
            currency=product.currency,
            total_amount=product.stars,
            payload_token_hash=intent.token_hash,
            status="fulfilled",
        )
        wallet = Wallet(user_id=user.id)
        session.add_all((receipt, wallet))
        await session.flush()
        lot = WalletLot(
            wallet_id=wallet.id,
            kind="purchased",
            payment_receipt_id=receipt.id,
            granted_credits=product.credits,
            available_credits=product.credits,
        )
        session.add(lot)
        await session.flush()
        return DebugPurchase(operator_id, receipt.id, lot.id, charge_id)


async def _refund_request(
    transactions: TransactionFactory,
    receipt_id: UUID,
) -> PaymentRefundRequest:
    async with transactions() as session:
        request = await session.scalar(
            select(PaymentRefundRequest).where(
                PaymentRefundRequest.payment_receipt_id == receipt_id
            )
        )
        assert request is not None
        session.expunge(request)
        return request


async def test_provider_acceptance_immediately_claws_back_and_is_idempotent(
    db_engine: AsyncEngine,
) -> None:
    transactions = _transactions(db_engine)
    purchase = await _create_fulfilled_debug_purchase(transactions)
    bot = _bot()
    service = OperatorDebugRefundService(
        transactions,
        bot,
        PaymentSettlementService(transactions),
    )

    result = await service.refund_latest(purchase.operator_id)
    repeated = await service.refund_latest(purchase.operator_id)

    assert result is OperatorDebugRefundResult.REQUESTED
    assert repeated is OperatorDebugRefundResult.REQUESTED
    bot.refund_star_payment.assert_awaited_once_with(
        user_id=purchase.operator_id,
        telegram_payment_charge_id=purchase.charge_id,
    )
    request = await _refund_request(transactions, purchase.receipt_id)
    assert request.status == "reconciled"
    assert request.attempt_count == 1
    assert request.accepted_at is not None
    assert request.reconciled_at is not None
    async with transactions() as session:
        receipt = await session.get(PaymentReceipt, purchase.receipt_id)
        lot = await session.get(WalletLot, purchase.wallet_lot_id)
        assert receipt is not None and receipt.status == "clawed_back"
        assert lot is not None
        assert lot.available_credits == 0
        assert lot.clawed_back_credits == lot.granted_credits


async def test_missing_debug_purchase_has_no_provider_effect(
    db_engine: AsyncEngine,
) -> None:
    transactions = _transactions(db_engine)
    bot = _bot()
    service = OperatorDebugRefundService(
        transactions,
        bot,
        PaymentSettlementService(transactions),
    )

    result = await service.refund_latest(_telegram_id())

    assert result is OperatorDebugRefundResult.NOT_FOUND
    bot.refund_star_payment.assert_not_awaited()


async def test_ambiguous_provider_result_is_never_retried(
    db_engine: AsyncEngine,
) -> None:
    transactions = _transactions(db_engine)
    purchase = await _create_fulfilled_debug_purchase(transactions)
    settlement = PaymentSettlementService(transactions)
    bot = _bot(outcome=TimeoutError("provider outcome unknown"))
    service = OperatorDebugRefundService(transactions, bot, settlement)

    result = await service.refund_latest(purchase.operator_id)
    repeated = await service.refund_latest(purchase.operator_id)
    sweep = await service.reconcile()

    assert result is OperatorDebugRefundResult.PENDING
    assert repeated is OperatorDebugRefundResult.PENDING
    assert sweep.processed == 0
    assert bot.refund_star_payment.await_count == 1
    request = await _refund_request(transactions, purchase.receipt_id)
    assert request.status == "needs_review"
    assert request.attempt_count == 1
    assert request.last_error_code == "TimeoutError"

    # A later Telegram refund update performs the exact-source clawback. A new
    # process then closes the command from local facts without another API call.
    await settlement.clawback(purchase.charge_id)
    restarted = OperatorDebugRefundService(transactions, bot, settlement)
    recovered = await restarted.reconcile()

    assert recovered.reconciled == 1
    assert bot.refund_star_payment.await_count == 1
    assert (await _refund_request(transactions, purchase.receipt_id)).status == (
        "reconciled"
    )


async def test_accepted_refund_reconciles_after_local_failure_and_restart(
    db_engine: AsyncEngine,
) -> None:
    transactions = _transactions(db_engine)
    purchase = await _create_fulfilled_debug_purchase(transactions)
    settlement = PaymentSettlementService(transactions)
    fail_once = FailOnceSettlement(settlement)
    bot = _bot()
    service = OperatorDebugRefundService(transactions, bot, fail_once)

    result = await service.refund_latest(purchase.operator_id)

    assert result is OperatorDebugRefundResult.PENDING
    assert fail_once.calls == 1
    assert (await _refund_request(transactions, purchase.receipt_id)).status == (
        "accepted"
    )

    restarted = OperatorDebugRefundService(transactions, bot, settlement)
    recovered = await restarted.reconcile()

    assert recovered.processed == 1
    assert recovered.reconciled == 1
    assert bot.refund_star_payment.await_count == 1
    request = await _refund_request(transactions, purchase.receipt_id)
    assert request.status == "reconciled"
    async with transactions() as session:
        receipt = await session.get(PaymentReceipt, purchase.receipt_id)
        assert receipt is not None and receipt.status == "clawed_back"
