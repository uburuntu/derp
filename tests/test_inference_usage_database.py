"""PostgreSQL integration contract for inference usage persistence."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from derp.inference_usage import (
    CostReconciliationStatus,
    InferenceCostReconciliation,
    InferenceOutcome,
    InferenceReconciliationClaimLostError,
    InferenceStatus,
    InferenceTokenUsage,
    InferenceUsageCompletion,
    InferenceUsageId,
    InferenceUsageRepository,
    InferenceUsageStart,
)


@pytest.mark.database
@pytest.mark.asyncio
async def test_usage_round_trip_preserves_uuid_scope_tokens_and_decimal_cost(
    db_session: AsyncSession,
    user_factory,
    chat_factory,
) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    user = await user_factory()
    chat = await chat_factory()
    user_id = user.id
    chat_id = chat.id

    @asynccontextmanager
    async def transactions():
        yield db_session

    repository = InferenceUsageRepository(
        transactions,
        clock=lambda: now + timedelta(seconds=2),
    )
    usage_id = InferenceUsageId.new()
    await repository.register(
        InferenceUsageStart(
            id=usage_id,
            operation_id=None,
            user_id=user_id,
            chat_id=chat_id,
            provider="provider-a",
            provider_model_id="model-v2",
            started_at=now,
        )
    )
    await repository.complete(
        usage_id,
        InferenceUsageCompletion(
            outcome=InferenceOutcome.SUCCEEDED,
            completed_at=now + timedelta(seconds=1),
            tokens=InferenceTokenUsage(
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                cache_read_tokens=75,
                cache_write_tokens=25,
                reasoning_tokens=12,
                audio_input_tokens=8,
                audio_output_tokens=4,
            ),
            provider_response_id="response-opaque",
            provider_generation_id="generation-opaque",
        ),
    )
    await repository.reconcile_cost(
        usage_id,
        InferenceCostReconciliation(
            actual_cost_usd=Decimal("0.012345678901"),
            reconciled_at=now + timedelta(seconds=3),
        ),
    )
    await db_session.flush()
    db_session.expire_all()

    stored = await repository.get(usage_id)

    assert stored.operation_id is None
    assert stored.user_id == user_id
    assert stored.chat_id == chat_id
    assert stored.status is InferenceStatus.SUCCEEDED
    assert stored.reconciliation_status is CostReconciliationStatus.RECONCILED
    assert stored.tokens == InferenceTokenUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_read_tokens=75,
        cache_write_tokens=25,
        reasoning_tokens=12,
        audio_input_tokens=8,
        audio_output_tokens=4,
    )
    assert stored.actual_cost_usd == Decimal("0.012345678901")
    assert stored.provider_response_id == "response-opaque"
    assert stored.provider_generation_id == "generation-opaque"


@pytest.mark.database
@pytest.mark.asyncio
async def test_reconciliation_lease_defers_reclaims_and_rejects_stale_owner(
    db_session: AsyncSession,
    user_factory,
) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    user = await user_factory()

    @asynccontextmanager
    async def transactions():
        yield db_session

    repository = InferenceUsageRepository(
        transactions,
        clock=lambda: now + timedelta(seconds=2),
    )
    usage_id = InferenceUsageId.new()
    await repository.register(
        InferenceUsageStart(
            id=usage_id,
            operation_id=None,
            user_id=user.id,
            chat_id=None,
            provider="openrouter",
            provider_model_id="provider/model",
            started_at=now,
        )
    )
    await repository.complete(
        usage_id,
        InferenceUsageCompletion(
            outcome=InferenceOutcome.SUCCEEDED,
            completed_at=now + timedelta(seconds=1),
            tokens=InferenceTokenUsage(input_tokens=100, output_tokens=50),
            provider_generation_id="generation-opaque",
        ),
    )

    first = (
        await repository.claim_pending_reconciliation(
            provider="openrouter",
            now=now + timedelta(seconds=3),
            lease_duration=timedelta(minutes=5),
            limit=1,
        )
    )[0]
    await repository.defer_cost_reconciliation(
        first,
        deferred_at=now + timedelta(seconds=4),
        retry_at=now + timedelta(minutes=10),
    )
    assert not await repository.claim_pending_reconciliation(
        provider="openrouter",
        now=now + timedelta(minutes=9),
        lease_duration=timedelta(minutes=5),
        limit=1,
    )
    second = (
        await repository.claim_pending_reconciliation(
            provider="openrouter",
            now=now + timedelta(minutes=10),
            lease_duration=timedelta(minutes=5),
            limit=1,
        )
    )[0]

    assert second.token != first.token
    assert second.attempt == 2
    with pytest.raises(InferenceReconciliationClaimLostError):
        await repository.reconcile_claimed_cost(
            first,
            InferenceCostReconciliation(
                actual_cost_usd=Decimal("0.01"),
                reconciled_at=now + timedelta(minutes=11),
            ),
        )

    reconciled = await repository.reconcile_claimed_cost(
        second,
        InferenceCostReconciliation(
            actual_cost_usd=Decimal("0.012345678901"),
            reconciled_at=now + timedelta(minutes=11),
        ),
    )
    assert reconciled.reconciliation_status is CostReconciliationStatus.RECONCILED
    assert reconciled.actual_cost_usd == Decimal("0.012345678901")
    assert reconciled.reconciliation_claim_token is None


@pytest.mark.database
@pytest.mark.asyncio
async def test_reconciliation_does_not_poll_records_without_generation_ids(
    db_session: AsyncSession,
    user_factory,
) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    user = await user_factory()

    @asynccontextmanager
    async def transactions():
        yield db_session

    repository = InferenceUsageRepository(transactions)
    usage_id = InferenceUsageId.new()
    await repository.register(
        InferenceUsageStart(
            id=usage_id,
            operation_id=None,
            user_id=user.id,
            chat_id=None,
            provider="openrouter",
            provider_model_id="provider/model",
            started_at=now,
        )
    )
    await repository.complete(
        usage_id,
        InferenceUsageCompletion(
            outcome=InferenceOutcome.SUCCEEDED,
            completed_at=now + timedelta(seconds=1),
        ),
    )

    claims = await repository.claim_pending_reconciliation(
        provider="openrouter",
        now=now + timedelta(minutes=1),
        lease_duration=timedelta(minutes=5),
    )

    assert claims == ()
    assert (await repository.get(usage_id)).reconciliation_attempts == 0
