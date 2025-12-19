"""Credit-specific database queries.

Provides atomic operations for credit management with proper locking
to prevent race conditions and double-spending.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import logfire
from sqlalchemy import Integer, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.models import Chat, CreditTransaction, DailyUsage, User

if TYPE_CHECKING:
    pass


# -----------------------------------------------------------------------------
# Balance Queries
# -----------------------------------------------------------------------------


async def get_user_credits(session: AsyncSession, user_id: UUID) -> int:
    """Get user's credit balance."""
    result = await session.execute(select(User.credits).where(User.id == user_id))
    return result.scalar_one_or_none() or 0


async def get_chat_credits(session: AsyncSession, chat_id: UUID) -> int:
    """Get chat's credit balance."""
    result = await session.execute(select(Chat.credits).where(Chat.id == chat_id))
    return result.scalar_one_or_none() or 0


async def get_balances(
    session: AsyncSession,
    user_telegram_id: int,
    chat_telegram_id: int,
) -> tuple[int, int]:
    """Get both chat and user credit balances.

    Returns:
        Tuple of (chat_credits, user_credits).
    """
    # Single query to get both balances
    user_credits = 0
    chat_credits = 0

    user_result = await session.execute(
        select(User.id, User.credits).where(User.telegram_id == user_telegram_id)
    )
    user_row = user_result.one_or_none()
    if user_row:
        user_credits = user_row.credits

    chat_result = await session.execute(
        select(Chat.id, Chat.credits).where(Chat.telegram_id == chat_telegram_id)
    )
    chat_row = chat_result.one_or_none()
    if chat_row:
        chat_credits = chat_row.credits

    return chat_credits, user_credits


# -----------------------------------------------------------------------------
# Credit Operations (with locking)
# -----------------------------------------------------------------------------


async def add_user_credits(
    session: AsyncSession,
    user_id: UUID,
    amount: int,
    transaction_type: str,
    *,
    telegram_charge_id: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Add credits to user balance atomically.

    Uses FOR UPDATE to lock the row and prevent race conditions.

    Returns:
        New balance after the operation.
    """
    with logfire.span(
        "db.add_user_credits",
        user_id=str(user_id),
        amount=amount,
        type=transaction_type,
    ):
        # Lock and get current balance
        result = await session.execute(
            select(User.credits).where(User.id == user_id).with_for_update()
        )
        current = result.scalar_one_or_none()
        if current is None:
            msg = f"User {user_id} not found while adding credits"
            raise ValueError(msg)
        new_balance = current + amount

        # Update balance
        await session.execute(
            update(User).where(User.id == user_id).values(credits=new_balance)
        )

        # Record transaction
        await session.execute(
            insert(CreditTransaction).values(
                user_id=user_id,
                chat_id=None,
                type=transaction_type,
                amount=amount,
                balance_after=new_balance,
                telegram_charge_id=telegram_charge_id,
                idempotency_key=idempotency_key,
                metadata_=metadata or {},
            )
        )

        return new_balance


async def deduct_user_credits(
    session: AsyncSession,
    user_id: UUID,
    amount: int,
    tool_name: str,
    model_id: str | None = None,
    *,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Deduct credits from user balance atomically.

    Returns:
        New balance after the operation.

    Raises:
        ValueError: If insufficient credits.
    """
    with logfire.span(
        "db.deduct_user_credits",
        user_id=str(user_id),
        amount=amount,
        tool=tool_name,
    ):
        # Lock and get current balance
        result = await session.execute(
            select(User.credits).where(User.id == user_id).with_for_update()
        )
        current = result.scalar_one_or_none()

        if current is None:
            msg = f"User {user_id} not found while deducting credits"
            raise ValueError(msg)

        if current < amount:
            msg = f"Insufficient user credits: {current} < {amount}"
            raise ValueError(msg)

        new_balance = current - amount

        # Update balance
        await session.execute(
            update(User).where(User.id == user_id).values(credits=new_balance)
        )

        # Record transaction
        await session.execute(
            insert(CreditTransaction).values(
                user_id=user_id,
                chat_id=None,
                type="spend",
                amount=-amount,
                balance_after=new_balance,
                tool_name=tool_name,
                model_id=model_id,
                idempotency_key=idempotency_key,
                metadata_=metadata or {},
            )
        )

        return new_balance


async def add_chat_credits(
    session: AsyncSession,
    chat_id: UUID,
    user_id: UUID,
    amount: int,
    transaction_type: str,
    *,
    telegram_charge_id: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Add credits to chat pool atomically.

    Returns:
        New balance after the operation.
    """
    with logfire.span(
        "db.add_chat_credits",
        chat_id=str(chat_id),
        user_id=str(user_id),
        amount=amount,
        type=transaction_type,
    ):
        # Lock and get current balance
        result = await session.execute(
            select(Chat.credits).where(Chat.id == chat_id).with_for_update()
        )
        current = result.scalar_one_or_none()
        if current is None:
            msg = f"Chat {chat_id} not found while adding credits"
            raise ValueError(msg)
        new_balance = current + amount

        # Update balance
        await session.execute(
            update(Chat).where(Chat.id == chat_id).values(credits=new_balance)
        )

        # Record transaction
        await session.execute(
            insert(CreditTransaction).values(
                user_id=user_id,
                chat_id=chat_id,
                type=transaction_type,
                amount=amount,
                balance_after=new_balance,
                telegram_charge_id=telegram_charge_id,
                idempotency_key=idempotency_key,
                metadata_=metadata or {},
            )
        )

        return new_balance


async def deduct_chat_credits(
    session: AsyncSession,
    chat_id: UUID,
    user_id: UUID,
    amount: int,
    tool_name: str,
    model_id: str | None = None,
    *,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Deduct credits from chat pool atomically.

    Returns:
        New balance after the operation.

    Raises:
        ValueError: If insufficient credits.
    """
    with logfire.span(
        "db.deduct_chat_credits",
        chat_id=str(chat_id),
        user_id=str(user_id),
        amount=amount,
        tool=tool_name,
    ):
        # Lock and get current balance
        result = await session.execute(
            select(Chat.credits).where(Chat.id == chat_id).with_for_update()
        )
        current = result.scalar_one_or_none()

        if current is None:
            msg = f"Chat {chat_id} not found while deducting credits"
            raise ValueError(msg)

        if current < amount:
            msg = f"Insufficient chat credits: {current} < {amount}"
            raise ValueError(msg)

        new_balance = current - amount

        # Update balance
        await session.execute(
            update(Chat).where(Chat.id == chat_id).values(credits=new_balance)
        )

        # Record transaction
        await session.execute(
            insert(CreditTransaction).values(
                user_id=user_id,
                chat_id=chat_id,
                type="spend",
                amount=-amount,
                balance_after=new_balance,
                tool_name=tool_name,
                model_id=model_id,
                idempotency_key=idempotency_key,
                metadata_=metadata or {},
            )
        )

        return new_balance


# -----------------------------------------------------------------------------
# Daily Usage Tracking
# -----------------------------------------------------------------------------


async def get_daily_usage(
    session: AsyncSession,
    user_id: UUID,
    chat_id: UUID,
    tool_name: str,
    usage_date: date | None = None,
) -> int:
    """Get usage count for a tool on a specific day.

    Args:
        session: Database session.
        user_id: User's UUID.
        chat_id: Chat's UUID.
        tool_name: Name of the tool.
        usage_date: Date to check (defaults to today UTC).

    Returns:
        Number of times the tool was used today.
    """
    usage_date = usage_date or datetime.now(UTC).date()

    result = await session.execute(
        select(DailyUsage.usage).where(
            DailyUsage.user_id == user_id,
            DailyUsage.chat_id == chat_id,
            DailyUsage.usage_date == usage_date,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return 0
    return row.get(tool_name, 0)


async def increment_daily_usage(
    session: AsyncSession,
    user_id: UUID,
    chat_id: UUID,
    tool_name: str,
    usage_date: date | None = None,
) -> int:
    """Increment usage count for a tool atomically.

    Uses upsert with JSONB update to handle concurrent increments.

    Returns:
        New usage count after increment.
    """
    usage_date = usage_date or datetime.now(UTC).date()

    with logfire.span(
        "db.increment_daily_usage",
        user_id=str(user_id),
        chat_id=str(chat_id),
        tool=tool_name,
        date=str(usage_date),
    ):
        # Upsert with atomic JSONB increment
        stmt = insert(DailyUsage).values(
            user_id=user_id,
            chat_id=chat_id,
            usage_date=usage_date,
            usage={tool_name: 1},
        )

        # On conflict, increment the specific key in JSONB
        increment_value = (
            func.coalesce(
                DailyUsage.usage[tool_name].astext.cast(Integer),
                0,
            )
            + 1
        )

        stmt = stmt.on_conflict_do_update(
            constraint="uq_daily_usage",
            set_={
                "usage": func.jsonb_set(
                    DailyUsage.usage,
                    func.array([tool_name]),
                    func.to_jsonb(increment_value),
                ),
                "updated_at": datetime.now(UTC),
            },
        ).returning(DailyUsage.usage)

        result = await session.execute(stmt)
        usage = result.scalar_one()
        return usage.get(tool_name, 1)


# -----------------------------------------------------------------------------
# Transaction Queries
# -----------------------------------------------------------------------------


async def get_transaction_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
) -> CreditTransaction | None:
    """Check if a transaction with this key already exists."""
    result = await session.execute(
        select(CreditTransaction).where(
            CreditTransaction.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()


async def get_user_transactions(
    session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
) -> list[CreditTransaction]:
    """Get recent transactions for a user."""
    result = await session.execute(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
