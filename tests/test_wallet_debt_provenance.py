"""Exact-source debt repayment and operation-reversal contracts."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.billing import (
    CapturedPayment,
    PaymentSettlementService,
    PreCheckoutRequest,
    PurchaseIntentService,
    PurchaseTarget,
)
from derp.catalog import GoogleModelKey, ImageResolution
from derp.execution import Feature, plan_execution
from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import (
    Chat,
    LegalAcceptance,
    User,
    Wallet,
    WalletDebtRepaymentAllocation,
    WalletDebtSource,
    WalletLedgerEntry,
    WalletLot,
)
from derp.operations import (
    ImageGenerateQuoteInput,
    OperationId,
    OperationLedger,
    QuoteEngine,
    QuoteId,
    ReservedOperation,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DebtEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    intents: PurchaseIntentService
    settlement: PaymentSettlementService
    ledger: OperationLedger


@pytest_asyncio.fixture
async def debt_env(db_engine: AsyncEngine) -> AsyncIterator[DebtEnvironment]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    counter = itertools.count(1)

    def clock() -> datetime:
        return NOW

    yield DebtEnvironment(
        transactions=transactions,
        intents=PurchaseIntentService(
            transactions,
            clock=clock,
            token_factory=lambda: f"debt-{next(counter)}-{uuid4().hex}",
        ),
        settlement=PaymentSettlementService(transactions, clock=clock),
        ledger=OperationLedger(transactions, clock=clock),
    )


async def _scope(env: DebtEnvironment) -> tuple[UUID, UUID, int]:
    telegram_id = 30_000_000 + uuid4().int % 8_000_000_000
    async with env.transactions() as session:
        user = User(telegram_id=telegram_id, is_bot=False, first_name="Debt")
        chat = Chat(telegram_id=telegram_id, type="private")
        session.add_all([user, chat])
        await session.flush()
        session.add(
            LegalAcceptance(
                user_id=user.id,
                document="terms",
                version=TERMS_ACCEPTANCE_VERSION,
                source="terms_command",
                accepted_at=NOW,
            )
        )
        return user.id, chat.id, telegram_id


async def _purchase(
    env: DebtEnvironment,
    user_id: UUID,
    telegram_id: int,
    charge_id: str,
    *,
    debug: bool = False,
):
    create = (
        env.intents.create_operator_debug_top_up_intent
        if debug
        else env.intents.create_top_up_intent
    )
    kwargs = {
        "payer_user_id": user_id,
        "target": PurchaseTarget.user(user_id),
    }
    if not debug:
        kwargs["product_id"] = "starter"
    handle = await create(**kwargs)
    decision = await env.intents.validate_pre_checkout(
        PreCheckoutRequest(
            handle.invoice_payload,
            telegram_id,
            "XTR",
            handle.stars,
        )
    )
    assert decision.approved
    result = await env.settlement.fulfill(
        CapturedPayment(
            handle.invoice_payload,
            charge_id,
            f"provider:{charge_id}",
            telegram_id,
            "XTR",
            handle.stars,
        )
    )
    return handle, result


async def _capture(
    env: DebtEnvironment,
    user_id: UUID,
    chat_id: UUID,
    amount_credits: int,
) -> OperationId:
    operation_id = OperationId(uuid4())
    priced = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(500, ImageResolution.ONE_K),
        created_at=NOW,
    )
    quote = replace(priced, credits=amount_credits)
    await env.ledger.register_quote(
        quote,
        request_key=f"debt:{operation_id}",
        requester_id=user_id,
        chat_id=chat_id,
        thread_id=None,
        pricing_input={"input_tokens": 500, "resolution": "1K"},
    )
    reservation = await env.ledger.reserve(operation_id)
    assert isinstance(reservation, ReservedOperation)
    await env.ledger.mark_executing(operation_id)
    await env.ledger.capture(operation_id)
    return operation_id


async def test_reversal_restores_the_grant_lot_that_repaid_refunded_spend(
    debt_env: DebtEnvironment,
) -> None:
    user_id, chat_id, telegram_id = await _scope(debt_env)
    source_charge = f"source-a-{uuid4().hex}"
    first_handle, first = await _purchase(debt_env, user_id, telegram_id, source_charge)
    operation_id = await _capture(debt_env, user_id, chat_id, 100)

    clawback = await debt_env.settlement.clawback(source_charge)
    second_handle, second = await _purchase(
        debt_env, user_id, telegram_id, f"repayment-b-{uuid4().hex}"
    )

    assert clawback.debt_created_credits == 100
    assert second.debt_offset_credits == 100
    assert second.available_credits == second_handle.credits - 100

    reversals = await asyncio.gather(
        debt_env.ledger.reverse(operation_id, reason="terminal_delivery_failure"),
        debt_env.ledger.reverse(operation_id, reason="terminal_delivery_failure"),
    )
    assert sum(result.changed for result in reversals) == 1

    async with debt_env.transactions() as session:
        wallet = await session.get(Wallet, first.wallet_id)
        source_lot = await session.get(WalletLot, first.wallet_lot_id)
        repayment_lot = await session.get(WalletLot, second.wallet_lot_id)
        debt_source = await session.scalar(
            select(WalletDebtSource).where(
                WalletDebtSource.source_wallet_lot_id == first.wallet_lot_id
            )
        )
        allocation = await session.scalar(
            select(WalletDebtRepaymentAllocation).where(
                WalletDebtRepaymentAllocation.debt_source_id == debt_source.id
            )
        )
        restored_event = await session.scalar(
            select(WalletLedgerEntry).where(
                WalletLedgerEntry.event_type == "debt_restored",
                WalletLedgerEntry.operation_id == operation_id.value,
                WalletLedgerEntry.wallet_lot_id == second.wallet_lot_id,
            )
        )

        assert wallet is not None and wallet.debt_credits == 0
        assert source_lot is not None
        assert source_lot.consumed_credits == 0
        assert source_lot.clawed_back_credits == first_handle.credits
        assert repayment_lot is not None
        assert repayment_lot.available_credits == second_handle.credits
        assert repayment_lot.debt_offset_credits == 0
        assert debt_source is not None
        assert debt_source.incurred_credits == debt_source.recovered_credits == 100
        assert debt_source.outstanding_credits == 0
        assert allocation is not None
        assert allocation.allocated_credits == allocation.restored_credits == 100
        assert allocation.revoked_credits == 0
        assert restored_event is not None
        assert restored_event.wallet_lot_id == second.wallet_lot_id
        assert restored_event.operation_id == operation_id.value
        assert restored_event.amount_credits == 100


async def test_partial_reversals_restore_multiple_repayment_lots_fifo(
    debt_env: DebtEnvironment,
) -> None:
    user_id, chat_id, telegram_id = await _scope(debt_env)
    source_charge = f"multi-source-{uuid4().hex}"
    _, source = await _purchase(debt_env, user_id, telegram_id, source_charge)
    first_operation = await _capture(debt_env, user_id, chat_id, 300)
    second_operation = await _capture(debt_env, user_id, chat_id, 200)
    await debt_env.settlement.clawback(source_charge)

    debug_handle, debug = await _purchase(
        debt_env,
        user_id,
        telegram_id,
        f"repayment-debug-{uuid4().hex}",
        debug=True,
    )
    starter_handle, starter = await _purchase(
        debt_env,
        user_id,
        telegram_id,
        f"repayment-starter-{uuid4().hex}",
    )
    assert debug.debt_offset_credits == debug_handle.credits == 10
    assert starter.debt_offset_credits == 490

    await debt_env.ledger.reverse(first_operation, reason="first_terminal_failure")
    async with debt_env.transactions() as session:
        debug_lot = await session.get(WalletLot, debug.wallet_lot_id)
        starter_lot = await session.get(WalletLot, starter.wallet_lot_id)
        source_row = await session.scalar(
            select(WalletDebtSource).where(
                WalletDebtSource.source_wallet_lot_id == source.wallet_lot_id
            )
        )
        allocations = list(
            await session.scalars(
                select(WalletDebtRepaymentAllocation)
                .where(WalletDebtRepaymentAllocation.debt_source_id == source_row.id)
                .order_by(
                    WalletDebtRepaymentAllocation.created_at,
                    WalletDebtRepaymentAllocation.repayment_wallet_lot_id,
                )
            )
        )
        assert debug_lot is not None and debug_lot.available_credits == 10
        assert debug_lot.debt_offset_credits == 0
        assert starter_lot is not None and starter_lot.available_credits == 400
        assert starter_lot.debt_offset_credits == 200
        assert source_row is not None and source_row.recovered_credits == 300
        assert source_row.outstanding_credits == 0
        assert sum(item.restored_credits for item in allocations) == 300
        assert (
            sum(
                item.allocated_credits - item.restored_credits - item.revoked_credits
                for item in allocations
            )
            == 200
        )

    await debt_env.ledger.reverse(second_operation, reason="second_terminal_failure")
    async with debt_env.transactions() as session:
        wallet = await session.get(Wallet, source.wallet_id)
        starter_lot = await session.get(WalletLot, starter.wallet_lot_id)
        source_row = await session.scalar(
            select(WalletDebtSource).where(
                WalletDebtSource.source_wallet_lot_id == source.wallet_lot_id
            )
        )
        allocations = list(
            await session.scalars(
                select(WalletDebtRepaymentAllocation).where(
                    WalletDebtRepaymentAllocation.debt_source_id == source_row.id
                )
            )
        )
        assert wallet is not None and wallet.debt_credits == 0
        assert starter_lot is not None
        assert starter_lot.available_credits == starter_handle.credits
        assert starter_lot.debt_offset_credits == 0
        assert source_row is not None
        assert source_row.recovered_credits == source_row.incurred_credits == 500
        assert all(
            item.allocated_credits == item.restored_credits + item.revoked_credits
            for item in allocations
        )


async def test_clawing_back_a_repayment_reopens_the_original_debt_source(
    debt_env: DebtEnvironment,
) -> None:
    user_id, chat_id, telegram_id = await _scope(debt_env)
    source_charge = f"reopen-source-{uuid4().hex}"
    repayment_charge = f"reopen-repayment-{uuid4().hex}"
    _, source = await _purchase(debt_env, user_id, telegram_id, source_charge)
    operation_id = await _capture(debt_env, user_id, chat_id, 100)
    await debt_env.settlement.clawback(source_charge)
    repayment_handle, repayment = await _purchase(
        debt_env,
        user_id,
        telegram_id,
        repayment_charge,
    )

    reopened = await debt_env.settlement.clawback(repayment_charge)
    assert reopened.removed_available_credits == repayment_handle.credits - 100
    assert reopened.debt_created_credits == 100

    async with debt_env.transactions() as session:
        wallet = await session.get(Wallet, source.wallet_id)
        source_row = await session.scalar(
            select(WalletDebtSource).where(
                WalletDebtSource.source_wallet_lot_id == source.wallet_lot_id
            )
        )
        allocation = await session.scalar(
            select(WalletDebtRepaymentAllocation).where(
                WalletDebtRepaymentAllocation.repayment_wallet_lot_id
                == repayment.wallet_lot_id
            )
        )
        repayment_lot = await session.get(WalletLot, repayment.wallet_lot_id)
        events = list(
            await session.scalars(
                select(WalletLedgerEntry.event_type).where(
                    WalletLedgerEntry.payment_receipt_id == repayment.receipt_id
                )
            )
        )
        assert wallet is not None and wallet.debt_credits == 100
        assert source_row is not None and source_row.outstanding_credits == 100
        assert allocation is not None
        assert allocation.revoked_credits == allocation.allocated_credits == 100
        assert repayment_lot is not None
        assert repayment_lot.debt_offset_credits == 0
        assert repayment_lot.clawed_back_credits == repayment_handle.credits
        assert "debt_reopened" in events

    await debt_env.ledger.reverse(operation_id, reason="terminal_delivery_failure")
    async with debt_env.transactions() as session:
        wallet = await session.get(Wallet, source.wallet_id)
        source_row = await session.scalar(
            select(WalletDebtSource).where(
                WalletDebtSource.source_wallet_lot_id == source.wallet_lot_id
            )
        )
        repayment_lot = await session.get(WalletLot, repayment.wallet_lot_id)
        assert wallet is not None and wallet.debt_credits == 0
        assert source_row is not None and source_row.recovered_credits == 100
        assert repayment_lot is not None
        assert repayment_lot.available_credits == 0
        assert repayment_lot.clawed_back_credits == repayment_handle.credits
