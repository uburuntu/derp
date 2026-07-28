"""State and idempotency tests for inference usage persistence."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from derp.inference_usage import (
    CostReconciliationStatus,
    InferenceCostReconciliation,
    InferenceCostReconciliationClaim,
    InferenceOutcome,
    InferenceReconciliationClaimLostError,
    InferenceStatus,
    InferenceTokenUsage,
    InferenceUsageCompletion,
    InferenceUsageConflictError,
    InferenceUsageId,
    InferenceUsageRepository,
    InferenceUsageStart,
    InvalidInferenceUsageTransitionError,
)
from derp.models.inference_usage import InferenceUsage

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _start() -> InferenceUsageStart:
    return InferenceUsageStart(
        id=InferenceUsageId.new(),
        operation_id=uuid4(),
        user_id=uuid4(),
        chat_id=uuid4(),
        provider="provider-a",
        provider_model_id="model-v2",
        started_at=NOW,
    )


def _stored(start: InferenceUsageStart) -> InferenceUsage:
    return InferenceUsage(
        id=start.id.value,
        operation_id=start.operation_id,
        user_id=start.user_id,
        chat_id=start.chat_id,
        provider=start.provider,
        provider_model_id=start.provider_model_id,
        provider_response_id=None,
        provider_generation_id=None,
        input_tokens=0,
        output_tokens=0,
        total_tokens=None,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        audio_input_tokens=0,
        audio_output_tokens=0,
        usage_available=False,
        actual_cost_usd=None,
        status=InferenceStatus.PENDING.value,
        reconciliation_status=CostReconciliationStatus.NOT_READY.value,
        provider_started_at=start.started_at,
        provider_completed_at=None,
        usage_recorded_at=None,
        reconciliation_requested_at=None,
        reconciliation_retry_at=None,
        reconciliation_claim_token=None,
        reconciliation_claimed_at=None,
        reconciliation_lease_expires_at=None,
        reconciliation_attempts=0,
        reconciled_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _transactions(session: AsyncSession):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _session(*, stored: InferenceUsage | None = None) -> AsyncSession:
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=stored and stored.id)
    session.get = AsyncMock(return_value=stored)
    session.scalars = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_register_is_idempotent_for_the_same_content_free_identity() -> None:
    start = _start()
    stored = _stored(start)
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))

    snapshot = await repository.register(start)

    assert snapshot.id == start.id
    assert snapshot.operation_id == start.operation_id
    assert snapshot.user_id == start.user_id
    assert snapshot.chat_id == start.chat_id
    assert snapshot.status is InferenceStatus.PENDING
    statement = session.scalar.await_args.args[0]
    assert statement.table.name == "inference_usage_records"
    session.get.assert_awaited_once_with(InferenceUsage, start.id.value)


@pytest.mark.asyncio
async def test_register_rejects_reused_id_for_different_actor_scope() -> None:
    start = _start()
    stored = _stored(start)
    stored.user_id = uuid4()
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))

    with pytest.raises(InferenceUsageConflictError, match=str(start.id)):
        await repository.register(start)


@pytest.mark.asyncio
async def test_complete_records_every_usage_category_and_queues_cost() -> None:
    start = _start()
    stored = _stored(start)
    session = _session(stored=stored)
    recorded_at = NOW + timedelta(seconds=2)
    repository = InferenceUsageRepository(
        _transactions(session),
        clock=lambda: recorded_at,
    )
    tokens = InferenceTokenUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_read_tokens=75,
        cache_write_tokens=25,
        reasoning_tokens=12,
        audio_input_tokens=8,
        audio_output_tokens=4,
    )
    completion = InferenceUsageCompletion(
        outcome=InferenceOutcome.FAILED,
        completed_at=NOW + timedelta(seconds=1),
        tokens=tokens,
        provider_response_id="response-1",
        provider_generation_id="generation-1",
    )

    snapshot = await repository.complete(start.id, completion)

    assert snapshot.status is InferenceStatus.FAILED
    assert snapshot.reconciliation_status is CostReconciliationStatus.PENDING
    assert snapshot.tokens == tokens
    assert snapshot.provider_response_id == "response-1"
    assert snapshot.provider_generation_id == "generation-1"
    assert snapshot.actual_cost_usd is None
    assert snapshot.usage_recorded_at == recorded_at
    assert snapshot.reconciliation_requested_at == recorded_at
    session.get.assert_awaited_once_with(
        InferenceUsage,
        start.id.value,
        with_for_update=True,
    )


@pytest.mark.asyncio
async def test_complete_is_idempotent_after_first_write() -> None:
    start = _start()
    stored = _stored(start)
    stored.status = InferenceStatus.SUCCEEDED.value
    stored.reconciliation_status = CostReconciliationStatus.PENDING.value
    stored.provider_completed_at = NOW + timedelta(seconds=1)
    stored.usage_recorded_at = NOW + timedelta(seconds=2)
    stored.reconciliation_requested_at = NOW + timedelta(seconds=2)
    stored.usage_available = False
    completion = InferenceUsageCompletion(
        outcome=InferenceOutcome.SUCCEEDED,
        completed_at=NOW + timedelta(seconds=10),
    )
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))

    snapshot = await repository.complete(start.id, completion)

    assert snapshot.provider_completed_at == NOW + timedelta(seconds=1)
    assert snapshot.tokens is None


@pytest.mark.asyncio
async def test_complete_rejects_invalid_repository_clock() -> None:
    start = _start()
    session = _session(stored=_stored(start))
    repository = InferenceUsageRepository(
        _transactions(session),
        clock=lambda: NOW - timedelta(seconds=1),
    )

    with pytest.raises(InvalidInferenceUsageTransitionError, match="recording time"):
        await repository.complete(
            start.id,
            InferenceUsageCompletion(
                outcome=InferenceOutcome.SUCCEEDED,
                completed_at=NOW,
            ),
        )


@pytest.mark.asyncio
async def test_reconcile_cost_is_exact_and_idempotent() -> None:
    start = _start()
    stored = _stored(start)
    stored.status = InferenceStatus.SUCCEEDED.value
    stored.reconciliation_status = CostReconciliationStatus.PENDING.value
    stored.provider_completed_at = NOW + timedelta(seconds=1)
    stored.usage_recorded_at = NOW + timedelta(seconds=2)
    stored.reconciliation_requested_at = NOW + timedelta(seconds=2)
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))
    reconciliation = InferenceCostReconciliation(
        actual_cost_usd=Decimal("0.012345678901"),
        reconciled_at=NOW + timedelta(seconds=3),
    )

    first = await repository.reconcile_cost(start.id, reconciliation)
    second = await repository.reconcile_cost(
        start.id,
        InferenceCostReconciliation(
            actual_cost_usd=reconciliation.actual_cost_usd,
            reconciled_at=NOW + timedelta(seconds=30),
        ),
    )

    assert first.actual_cost_usd == Decimal("0.012345678901")
    assert first.reconciliation_status is CostReconciliationStatus.RECONCILED
    assert first.reconciled_at == NOW + timedelta(seconds=3)
    assert second == first


@pytest.mark.asyncio
async def test_reconcile_rejects_pending_provider_attempt() -> None:
    start = _start()
    session = _session(stored=_stored(start))
    repository = InferenceUsageRepository(_transactions(session))

    with pytest.raises(InvalidInferenceUsageTransitionError, match="not ready"):
        await repository.reconcile_cost(
            start.id,
            InferenceCostReconciliation(
                actual_cost_usd=Decimal(0),
                reconciled_at=NOW,
            ),
        )


@pytest.mark.asyncio
async def test_pending_reconciliation_query_is_bounded_and_ordered() -> None:
    start = _start()
    stored = _stored(start)
    stored.status = InferenceStatus.SUCCEEDED.value
    stored.reconciliation_status = CostReconciliationStatus.PENDING.value
    stored.provider_completed_at = NOW
    stored.usage_recorded_at = NOW
    stored.reconciliation_requested_at = NOW
    session = _session()
    scalars = MagicMock()
    scalars.all.return_value = [stored]
    session.scalars.return_value = scalars
    repository = InferenceUsageRepository(_transactions(session))

    snapshots = await repository.list_pending_reconciliation(
        requested_before=NOW + timedelta(minutes=1),
        limit=25,
    )

    assert snapshots[0].id == start.id
    statement = session.scalars.await_args.args[0]
    assert statement._limit_clause.value == 25
    assert len(statement._order_by_clauses) == 2


@pytest.mark.parametrize("limit", [False, 0, 1001])
@pytest.mark.asyncio
async def test_pending_reconciliation_query_rejects_unbounded_limits(
    limit: object,
) -> None:
    repository = InferenceUsageRepository(_transactions(_session()))

    with pytest.raises(ValueError, match="between 1 and 1000"):
        await repository.list_pending_reconciliation(
            requested_before=NOW,
            limit=limit,  # type: ignore[arg-type]
        )


def _pending_reconciliation(start: InferenceUsageStart) -> InferenceUsage:
    stored = _stored(start)
    stored.status = InferenceStatus.SUCCEEDED.value
    stored.reconciliation_status = CostReconciliationStatus.PENDING.value
    stored.provider_generation_id = "generation-1"
    stored.provider_completed_at = NOW + timedelta(seconds=1)
    stored.usage_recorded_at = NOW + timedelta(seconds=2)
    stored.reconciliation_requested_at = NOW + timedelta(seconds=2)
    stored.usage_available = True
    stored.input_tokens = 100
    stored.output_tokens = 50
    stored.total_tokens = 150
    return stored


def _claim(stored: InferenceUsage) -> InferenceCostReconciliationClaim:
    token = uuid4()
    stored.reconciliation_claim_token = token
    stored.reconciliation_claimed_at = NOW + timedelta(seconds=3)
    stored.reconciliation_lease_expires_at = NOW + timedelta(minutes=5)
    stored.reconciliation_attempts = 1
    return InferenceCostReconciliationClaim(
        usage_id=InferenceUsageId(stored.id),
        token=token,
        provider_generation_id=stored.provider_generation_id,
        expected_tokens=InferenceTokenUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
        claimed_at=stored.reconciliation_claimed_at,
        lease_expires_at=stored.reconciliation_lease_expires_at,
        attempt=1,
    )


@pytest.mark.asyncio
async def test_claim_pending_reconciliation_leases_skip_locked_batch() -> None:
    start = _start()
    stored = _pending_reconciliation(start)
    stored.reconciliation_retry_at = NOW + timedelta(seconds=2)
    session = _session()
    scalars = MagicMock()
    scalars.all.return_value = [stored]
    session.scalars.return_value = scalars
    repository = InferenceUsageRepository(_transactions(session))
    claimed_at = NOW + timedelta(seconds=3)

    claims = await repository.claim_pending_reconciliation(
        provider="provider-a",
        now=claimed_at,
        lease_duration=timedelta(minutes=5),
        limit=25,
    )

    assert len(claims) == 1
    assert claims[0].usage_id == start.id
    assert claims[0].provider_generation_id == "generation-1"
    assert claims[0].expected_tokens == InferenceTokenUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
    )
    assert claims[0].attempt == 1
    assert stored.reconciliation_retry_at is None
    statement = session.scalars.await_args.args[0]
    assert statement._limit_clause.value == 25
    assert statement._for_update_arg.skip_locked is True


@pytest.mark.asyncio
async def test_claimed_cost_requires_owner_and_clears_lease() -> None:
    stored = _pending_reconciliation(_start())
    claim = _claim(stored)
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))

    snapshot = await repository.reconcile_claimed_cost(
        claim,
        InferenceCostReconciliation(
            actual_cost_usd=Decimal("0.012345678901"),
            reconciled_at=NOW + timedelta(seconds=4),
        ),
    )

    assert snapshot.actual_cost_usd == Decimal("0.012345678901")
    assert snapshot.reconciliation_status is CostReconciliationStatus.RECONCILED
    assert snapshot.reconciliation_claim_token is None
    assert snapshot.reconciliation_lease_expires_at is None


@pytest.mark.asyncio
async def test_defer_releases_owner_and_schedules_retry() -> None:
    stored = _pending_reconciliation(_start())
    claim = _claim(stored)
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))
    deferred_at = NOW + timedelta(seconds=4)
    retry_at = NOW + timedelta(minutes=10)

    snapshot = await repository.defer_cost_reconciliation(
        claim,
        deferred_at=deferred_at,
        retry_at=retry_at,
    )

    assert snapshot.reconciliation_status is CostReconciliationStatus.PENDING
    assert snapshot.reconciliation_retry_at == retry_at
    assert snapshot.reconciliation_claim_token is None
    assert snapshot.reconciliation_attempts == 1


@pytest.mark.asyncio
async def test_terminal_unavailable_requires_owner_and_clears_lease() -> None:
    stored = _pending_reconciliation(_start())
    claim = _claim(stored)
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))

    snapshot = await repository.mark_claimed_cost_unavailable(
        claim,
        unavailable_at=NOW + timedelta(seconds=4),
    )

    assert snapshot.reconciliation_status is CostReconciliationStatus.UNAVAILABLE
    assert snapshot.reconciliation_requested_at == NOW + timedelta(seconds=2)
    assert snapshot.reconciliation_attempts == 1
    assert snapshot.reconciliation_retry_at is None
    assert snapshot.reconciliation_claim_token is None
    assert snapshot.reconciliation_claimed_at is None
    assert snapshot.reconciliation_lease_expires_at is None


@pytest.mark.asyncio
async def test_stale_pending_attempt_cleanup_is_bounded_and_terminal() -> None:
    stored = _stored(_start())
    session = _session()
    scalars = MagicMock()
    scalars.all.return_value = [stored]
    session.scalars.return_value = scalars
    repository = InferenceUsageRepository(_transactions(session))
    failed_at = NOW + timedelta(hours=2)

    count = await repository.fail_stale_attempts(
        provider="provider-a",
        started_before=NOW + timedelta(hours=1),
        failed_at=failed_at,
        limit=25,
    )

    assert count == 1
    assert stored.status == InferenceStatus.FAILED.value
    assert stored.provider_completed_at == failed_at
    assert stored.usage_recorded_at == failed_at
    assert stored.reconciliation_status == CostReconciliationStatus.UNAVAILABLE.value
    statement = session.scalars.await_args.args[0]
    assert statement._limit_clause.value == 25
    assert statement._for_update_arg.skip_locked is True


@pytest.mark.asyncio
async def test_stale_worker_cannot_reconcile_reclaimed_record() -> None:
    stored = _pending_reconciliation(_start())
    stale_claim = _claim(stored)
    stored.reconciliation_claim_token = uuid4()
    stored.reconciliation_attempts = 2
    session = _session(stored=stored)
    repository = InferenceUsageRepository(_transactions(session))

    with pytest.raises(InferenceReconciliationClaimLostError):
        await repository.reconcile_claimed_cost(
            stale_claim,
            InferenceCostReconciliation(
                actual_cost_usd=Decimal("0.01"),
                reconciled_at=NOW + timedelta(seconds=4),
            ),
        )


@pytest.mark.parametrize("limit", [False, 0, 101])
@pytest.mark.asyncio
async def test_claim_query_rejects_unbounded_batch(limit: object) -> None:
    repository = InferenceUsageRepository(_transactions(_session()))

    with pytest.raises((TypeError, ValueError)):
        await repository.claim_pending_reconciliation(
            provider="openrouter",
            now=NOW,
            lease_duration=timedelta(minutes=5),
            limit=limit,  # type: ignore[arg-type]
        )
