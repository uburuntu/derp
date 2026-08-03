"""PostgreSQL contracts for bounded paid-operation crash reconciliation."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.artifacts import FilesystemArtifactStore
from derp.delivery import DeliveryService, ResendTokenCodec
from derp.models import (
    Artifact,
    Chat,
    DeliveryIntent,
    OperationAllocation,
    OperationQuote,
    PaidOperation,
    User,
    Wallet,
    WalletLot,
)
from derp.operations import (
    DeliveryState,
    OperationId,
    OperationLedger,
    OperationReconciler,
    OperationState,
)

pytestmark = pytest.mark.database

NOW = datetime(2001, 7, 20, 15, tzinfo=UTC)
STALE_AFTER = timedelta(minutes=30)
AMOUNT = 3

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class ReconciliationEnvironment:
    transactions: TransactionFactory
    reconciler: OperationReconciler
    ledger: OperationLedger


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: OperationId
    lot_id: UUID | None


@pytest_asyncio.fixture
async def reconciliation_env(
    db_engine: AsyncEngine,
    tmp_path,
) -> AsyncIterator[ReconciliationEnvironment]:
    async with db_engine.connect() as connection:
        outer_transaction = await connection.begin()
        sessions = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

        @asynccontextmanager
        async def transactions() -> AsyncIterator[AsyncSession]:
            async with sessions() as session, session.begin():
                yield session

        bot = MagicMock(spec=Bot)
        reversal = MagicMock()
        reversal.reverse = AsyncMock()
        delivery = DeliveryService(
            transactions,
            FilesystemArtifactStore(tmp_path / "reconciliation-artifacts"),
            bot,
            reversal,
            ResendTokenCodec(b"operation-reconciliation-tests".ljust(32, b"!")),
            clock=lambda: NOW,
        )
        try:
            yield ReconciliationEnvironment(
                transactions=transactions,
                reconciler=OperationReconciler(
                    transactions,
                    delivery,
                    clock=lambda: NOW,
                    stale_after=STALE_AFTER,
                ),
                ledger=OperationLedger(transactions, clock=lambda: NOW),
            )
        finally:
            await outer_transaction.rollback()


def _telegram_id() -> int:
    return 10_000_000 + uuid4().int % 9_000_000_000


async def _create_operation(
    env: ReconciliationEnvironment,
    state: OperationState,
    *,
    age: timedelta = timedelta(0),
    quote_expired: bool = False,
    with_artifact: bool = False,
    with_intent: bool | None = None,
    delivery_state: DeliveryState = DeliveryState.NOT_READY,
) -> OperationRecord:
    operation_id = OperationId(uuid4())
    event_at = NOW - age
    if with_intent is None:
        with_intent = with_artifact
    async with env.transactions() as session:
        user = User(
            telegram_id=_telegram_id(),
            is_bot=False,
            first_name="Reconciliation",
        )
        chat = Chat(
            telegram_id=-_telegram_id(),
            type="supergroup",
        )
        session.add_all([user, chat])
        await session.flush()
        quote_created_at = NOW - timedelta(minutes=20)
        quote = OperationQuote(
            operation_id=operation_id.value,
            request_key=f"operation-reconciliation:{operation_id}",
            requester_id=user.id,
            chat_id=chat.id,
            feature="image_generate",
            model_key="image",
            provider_model_id="gemini-reconciliation-test",
            context_band="small",
            variant="resolution=1K",
            amount_credits=AMOUNT,
            estimated_provider_cost_usd=Decimal("0.04"),
            pricing_version="reconciliation-test-v1",
            catalog_verified_on=date(2001, 7, 20),
            pricing_input={"resolution": "1K"},
            expires_at=(
                NOW - timedelta(minutes=1) if quote_expired else NOW + timedelta(days=1)
            ),
            created_at=quote_created_at,
        )
        session.add(quote)
        await session.flush()

        lot_id: UUID | None = None
        wallet_id: UUID | None = None
        if state is not OperationState.QUOTED:
            wallet = Wallet(user_id=user.id)
            session.add(wallet)
            await session.flush()
            wallet_id = wallet.id
            captured = state is OperationState.CAPTURED
            lot = WalletLot(
                wallet_id=wallet.id,
                kind="purchased",
                granted_credits=20,
                available_credits=20 - AMOUNT,
                reserved_credits=0 if captured else AMOUNT,
                consumed_credits=AMOUNT if captured else 0,
                expired_credits=0,
                clawed_back_credits=0,
                debt_offset_credits=0,
            )
            session.add(lot)
            await session.flush()
            lot_id = lot.id

        operation = PaidOperation(
            id=operation_id.value,
            quote_id=quote.id,
            wallet_id=wallet_id,
            funding_authorization="private" if wallet_id else None,
            state=state.value,
            delivery_state=delivery_state.value,
            reserved_at=(
                event_at
                if state
                in {
                    OperationState.RESERVED,
                    OperationState.EXECUTING,
                    OperationState.CAPTURED,
                }
                else None
            ),
            execution_started_at=(
                event_at
                if state in {OperationState.EXECUTING, OperationState.CAPTURED}
                else None
            ),
            captured_at=event_at if state is OperationState.CAPTURED else None,
            created_at=event_at,
            updated_at=event_at,
        )
        session.add(operation)
        await session.flush()
        if lot_id is not None:
            session.add(
                OperationAllocation(
                    operation_id=operation.id,
                    wallet_lot_id=lot_id,
                    wallet_id=wallet_id,
                    amount_credits=AMOUNT,
                )
            )

        if with_artifact:
            digest = hashlib.sha256(operation.id.bytes).hexdigest()
            session.add(
                Artifact(
                    operation_id=operation.id,
                    ordinal=0,
                    kind="image",
                    mime_type="image/png",
                    filename="result.png",
                    storage_key=operation.id.hex,
                    size_bytes=4,
                    sha256=digest,
                    expires_at=NOW + timedelta(hours=6),
                    created_at=event_at,
                )
            )
        if with_intent:
            token_hash = hashlib.sha256(b"resend:" + operation.id.bytes).hexdigest()
            session.add(
                DeliveryIntent(
                    operation_id=operation.id,
                    chat_id=-_telegram_id(),
                    resend_token_hash=token_hash,
                    state=delivery_state.value,
                    expires_at=NOW + timedelta(hours=6),
                    created_at=event_at,
                    updated_at=event_at,
                )
            )
    return OperationRecord(operation_id, lot_id)


async def _operation_state(
    env: ReconciliationEnvironment,
    record: OperationRecord,
) -> tuple[OperationState, DeliveryState]:
    async with env.transactions() as session:
        operation = await session.get(PaidOperation, record.operation_id.value)
        assert operation is not None
        return OperationState(operation.state), DeliveryState(operation.delivery_state)


async def _lot_counters(
    env: ReconciliationEnvironment,
    record: OperationRecord,
) -> tuple[int, int, int]:
    assert record.lot_id is not None
    async with env.transactions() as session:
        lot = await session.get(WalletLot, record.lot_id)
        assert lot is not None
        return lot.available_credits, lot.reserved_credits, lot.consumed_credits


async def test_reconciles_every_crash_boundary_without_resuming_provider_work(
    reconciliation_env: ReconciliationEnvironment,
) -> None:
    env = reconciliation_env
    expired_quote = await _create_operation(
        env,
        OperationState.QUOTED,
        quote_expired=True,
    )
    stale_reserved = await _create_operation(
        env,
        OperationState.RESERVED,
        age=timedelta(minutes=31),
    )
    active_reserved = await _create_operation(
        env,
        OperationState.RESERVED,
        age=timedelta(minutes=29),
    )
    stale_execution = await _create_operation(
        env,
        OperationState.EXECUTING,
        age=timedelta(minutes=31),
    )
    active_execution = await _create_operation(
        env,
        OperationState.EXECUTING,
        age=timedelta(minutes=29),
    )
    persisted_execution = await _create_operation(
        env,
        OperationState.EXECUTING,
        age=timedelta(minutes=31),
        with_artifact=True,
    )
    captured_not_ready = await _create_operation(
        env,
        OperationState.CAPTURED,
        age=timedelta(minutes=1),
        with_artifact=True,
    )
    captured_pending = await _create_operation(
        env,
        OperationState.CAPTURED,
        age=timedelta(minutes=31),
        with_artifact=True,
        delivery_state=DeliveryState.PENDING,
    )

    report = await env.reconciler.reconcile(limit=100)

    assert report.examined_count == 5
    assert report.expired_quote_count == 1
    assert report.released_reservation_count == 1
    assert report.released_execution_count == 1
    assert report.recovered_delivery_count == 2
    assert report.race_skipped_count == 0

    assert await _operation_state(env, expired_quote) == (
        OperationState.CANCELED,
        DeliveryState.NOT_READY,
    )
    assert await _operation_state(env, stale_reserved) == (
        OperationState.RELEASED,
        DeliveryState.NOT_READY,
    )
    assert await _lot_counters(env, stale_reserved) == (20, 0, 0)
    assert await _operation_state(env, stale_execution) == (
        OperationState.RELEASED,
        DeliveryState.NOT_READY,
    )
    assert await _lot_counters(env, stale_execution) == (20, 0, 0)
    assert await _operation_state(env, persisted_execution) == (
        OperationState.CAPTURED,
        DeliveryState.PENDING,
    )
    assert await _lot_counters(env, persisted_execution) == (17, 0, 3)
    assert await _operation_state(env, captured_not_ready) == (
        OperationState.CAPTURED,
        DeliveryState.PENDING,
    )
    assert await _operation_state(env, captured_pending) == (
        OperationState.CAPTURED,
        DeliveryState.PENDING,
    )
    assert await _operation_state(env, active_reserved) == (
        OperationState.RESERVED,
        DeliveryState.NOT_READY,
    )
    assert await _operation_state(env, active_execution) == (
        OperationState.EXECUTING,
        DeliveryState.NOT_READY,
    )

    assert await env.reconciler.reconcile(limit=100) == type(report).empty()


async def test_batch_limit_is_global_and_reruns_are_idempotent(
    reconciliation_env: ReconciliationEnvironment,
) -> None:
    records = [
        await _create_operation(
            reconciliation_env,
            OperationState.QUOTED,
            quote_expired=True,
        )
        for _ in range(3)
    ]

    first = await reconciliation_env.reconciler.reconcile(limit=2)
    second = await reconciliation_env.reconciler.reconcile(limit=2)
    third = await reconciliation_env.reconciler.reconcile(limit=2)

    assert first.examined_count == first.expired_quote_count == 2
    assert second.examined_count == second.expired_quote_count == 1
    assert third.reconciled_count == third.examined_count == 0
    assert [
        (await _operation_state(reconciliation_env, record))[0] for record in records
    ] == [OperationState.CANCELED] * 3


async def test_conditional_transitions_recheck_state_and_durable_results(
    reconciliation_env: ReconciliationEnvironment,
) -> None:
    reserved = await _create_operation(
        reconciliation_env,
        OperationState.RESERVED,
        age=timedelta(minutes=31),
    )
    await reconciliation_env.ledger.mark_executing(reserved.operation_id)

    stale_release = await reconciliation_env.ledger.release_stale_reservation(
        reserved.operation_id,
        stale_before=NOW - STALE_AFTER,
        reason="race_recheck",
    )

    assert stale_release is None
    assert (await _operation_state(reconciliation_env, reserved))[0] is (
        OperationState.EXECUTING
    )

    completed = await _create_operation(
        reconciliation_env,
        OperationState.EXECUTING,
        age=timedelta(minutes=31),
        with_artifact=True,
    )
    orphan_release = (
        await reconciliation_env.ledger.release_stale_execution_without_result(
            completed.operation_id,
            stale_before=NOW - STALE_AFTER,
            reason="race_recheck",
        )
    )

    assert orphan_release is None
    assert (await _operation_state(reconciliation_env, completed))[0] is (
        OperationState.EXECUTING
    )


async def test_stale_artifact_without_delivery_intent_is_surfaced_not_released(
    reconciliation_env: ReconciliationEnvironment,
) -> None:
    incomplete = await _create_operation(
        reconciliation_env,
        OperationState.EXECUTING,
        age=timedelta(minutes=31),
        with_artifact=True,
        with_intent=False,
    )

    report = await reconciliation_env.reconciler.reconcile(limit=100)

    assert report.reconciled_count == 0
    assert report.examined_count == report.incomplete_result_count == 1
    assert (await _operation_state(reconciliation_env, incomplete))[0] is (
        OperationState.EXECUTING
    )
    assert await _lot_counters(reconciliation_env, incomplete) == (17, 3, 0)
