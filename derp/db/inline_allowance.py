"""Atomic PostgreSQL admission for bounded free inline answers."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import date

import logfire
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.features.inline_chat import (
    InlineAllowanceClaim,
    InlineAllowanceExhausted,
    InlineAllowanceGranted,
)
from derp.models import InlineDailyAllowance

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class PostgresInlineAllowance:
    """Claim provider attempts with one conditional PostgreSQL upsert."""

    def __init__(self, transactions: TransactionFactory) -> None:
        if not callable(transactions):
            raise TypeError("transactions must be callable")
        self._transactions = transactions

    async def claim(
        self,
        *,
        user_id: uuid.UUID,
        usage_date: date,
        limit: int,
    ) -> InlineAllowanceClaim:
        """Atomically increment only while the UTC-day count is below ``limit``."""
        if not isinstance(user_id, uuid.UUID):
            raise TypeError("user_id must be a UUID")
        if type(usage_date) is not date:
            raise TypeError("usage_date must be a date")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            raise ValueError("limit must be positive")

        with logfire.span(
            "db.inline_allowance.claim",
            **{
                "db.operation": "upsert",
                "db.system": "postgresql",
            },
        ) as span:
            async with self._transactions() as session:
                statement = (
                    insert(InlineDailyAllowance)
                    .values(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        usage_date=usage_date,
                        used_count=1,
                    )
                    .on_conflict_do_update(
                        constraint="uq_inline_daily_allowance_user_date",
                        set_={
                            "used_count": InlineDailyAllowance.used_count + 1,
                            "updated_at": func.now(),
                        },
                        where=InlineDailyAllowance.used_count < limit,
                    )
                    .returning(InlineDailyAllowance.used_count)
                )
                used_count = await session.scalar(statement)
                if used_count is not None:
                    span.set_attribute("db.rows_returned", 1)
                    span.set_attribute("derp.inline.allowance_granted", True)
                    return InlineAllowanceGranted(usage_date, used_count, limit)

                stored_count = await session.scalar(
                    select(InlineDailyAllowance.used_count).where(
                        InlineDailyAllowance.user_id == user_id,
                        InlineDailyAllowance.usage_date == usage_date,
                    )
                )
                if stored_count is None:
                    raise RuntimeError("inline allowance conflict row disappeared")
                span.set_attribute("db.rows_returned", 1)
                span.set_attribute("derp.inline.allowance_granted", False)
                return InlineAllowanceExhausted(
                    usage_date,
                    stored_count,
                    limit,
                )


__all__ = ["PostgresInlineAllowance", "TransactionFactory"]
