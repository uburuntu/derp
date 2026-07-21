"""PostgreSQL concurrency contracts for bounded free inline usage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from derp.db.inline_allowance import PostgresInlineAllowance
from derp.features.inline_chat import (
    InlineAllowanceExhausted,
    InlineAllowanceGranted,
)
from derp.models import InlineDailyAllowance, User

pytestmark = pytest.mark.database

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@pytest_asyncio.fixture
async def allowance_env(
    db_engine: AsyncEngine,
) -> AsyncIterator[tuple[TransactionFactory, PostgresInlineAllowance]]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    yield transactions, PostgresInlineAllowance(transactions)


async def _create_user(transactions: TransactionFactory) -> UUID:
    async with transactions() as session:
        user = User(
            telegram_id=10_000_000 + uuid4().int % 8_000_000_000,
            is_bot=False,
            first_name="Inline",
        )
        session.add(user)
        await session.flush()
        return user.id


async def test_concurrent_claims_never_exceed_daily_limit(
    allowance_env: tuple[TransactionFactory, PostgresInlineAllowance],
) -> None:
    transactions, allowance = allowance_env
    user_id = await _create_user(transactions)
    usage_date = date(2026, 7, 21)

    outcomes = await asyncio.gather(
        *(
            allowance.claim(user_id=user_id, usage_date=usage_date, limit=7)
            for _ in range(32)
        )
    )

    granted = [item for item in outcomes if isinstance(item, InlineAllowanceGranted)]
    exhausted = [
        item for item in outcomes if isinstance(item, InlineAllowanceExhausted)
    ]
    assert len(granted) == 7
    assert len(exhausted) == 25
    assert {item.used_count for item in granted} == set(range(1, 8))
    async with transactions() as session:
        assert (
            await session.scalar(
                select(InlineDailyAllowance.used_count).where(
                    InlineDailyAllowance.user_id == user_id,
                    InlineDailyAllowance.usage_date == usage_date,
                )
            )
            == 7
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InlineDailyAllowance)
                .where(InlineDailyAllowance.user_id == user_id)
            )
            == 1
        )


async def test_new_utc_day_has_an_independent_allowance(
    allowance_env: tuple[TransactionFactory, PostgresInlineAllowance],
) -> None:
    transactions, allowance = allowance_env
    user_id = await _create_user(transactions)

    first = await allowance.claim(
        user_id=user_id,
        usage_date=date(2026, 7, 21),
        limit=1,
    )
    exhausted = await allowance.claim(
        user_id=user_id,
        usage_date=date(2026, 7, 21),
        limit=1,
    )
    next_day = await allowance.claim(
        user_id=user_id,
        usage_date=date(2026, 7, 22),
        limit=1,
    )

    assert first == InlineAllowanceGranted(date(2026, 7, 21), 1, 1)
    assert exhausted == InlineAllowanceExhausted(date(2026, 7, 21), 1, 1)
    assert next_day == InlineAllowanceGranted(date(2026, 7, 22), 1, 1)


async def test_deleting_user_cascades_inline_allowance(
    allowance_env: tuple[TransactionFactory, PostgresInlineAllowance],
) -> None:
    transactions, allowance = allowance_env
    user_id = await _create_user(transactions)
    await allowance.claim(
        user_id=user_id,
        usage_date=date(2026, 7, 21),
        limit=1,
    )

    async with transactions() as session:
        user = await session.get(User, user_id)
        assert user is not None
        await session.delete(user)

    async with transactions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InlineDailyAllowance)
                .where(InlineDailyAllowance.user_id == user_id)
            )
            == 0
        )


async def test_migrated_schema_rejects_negative_usage(
    allowance_env: tuple[TransactionFactory, PostgresInlineAllowance],
) -> None:
    transactions, _ = allowance_env
    user_id = await _create_user(transactions)

    with pytest.raises(IntegrityError):
        async with transactions() as session:
            session.add(
                InlineDailyAllowance(
                    user_id=user_id,
                    usage_date=date(2026, 7, 21),
                    used_count=-1,
                )
            )
            await session.flush()
