"""PostgreSQL concurrency contract for inference reconciliation leases."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.inference_usage import (
    CostReconciliationStatus,
    InferenceOutcome,
    InferenceTokenUsage,
    InferenceUsageCompletion,
    InferenceUsageId,
    InferenceUsageRepository,
    InferenceUsageStart,
)
from derp.models import User
from derp.models.inference_usage import InferenceUsage

pytestmark = pytest.mark.database


async def test_concurrent_workers_claim_one_pending_record_once(
    db_engine: AsyncEngine,
) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    async with transactions() as session:
        user = User(
            telegram_id=10_000_000_000 + uuid4().int % 8_000_000_000,
            is_bot=False,
            first_name="Reconciliation",
        )
        session.add(user)
        await session.flush()
        user_id = user.id

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

    batches = await asyncio.gather(
        *(
            repository.claim_pending_reconciliation(
                provider="openrouter",
                now=now + timedelta(seconds=3),
                lease_duration=timedelta(minutes=5),
                limit=1,
            )
            for _ in range(8)
        )
    )
    claims = tuple(claim for batch in batches for claim in batch)

    assert len(claims) == 1
    assert claims[0].attempt == 1
    assert not await repository.claim_pending_reconciliation(
        provider="openrouter",
        now=now + timedelta(minutes=4),
        lease_duration=timedelta(minutes=5),
        limit=1,
    )
    reclaimed = await repository.claim_pending_reconciliation(
        provider="openrouter",
        now=now + timedelta(minutes=6),
        lease_duration=timedelta(minutes=5),
        limit=1,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt == 2
    assert reclaimed[0].token != claims[0].token

    terminal = await repository.mark_claimed_cost_unavailable(
        reclaimed[0],
        unavailable_at=now + timedelta(minutes=7),
    )
    assert terminal.reconciliation_status is CostReconciliationStatus.UNAVAILABLE
    assert terminal.reconciliation_attempts == 2
    assert terminal.reconciliation_claim_token is None
    assert not await repository.claim_pending_reconciliation(
        provider="openrouter",
        now=now + timedelta(days=1),
        lease_duration=timedelta(minutes=5),
        limit=1,
    )

    async with transactions() as session:
        await session.execute(
            delete(InferenceUsage).where(InferenceUsage.id == usage_id.value)
        )
        await session.execute(delete(User).where(User.id == user_id))
