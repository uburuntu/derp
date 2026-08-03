"""Atomic persistence commands for topic-scoped, untrusted shared facts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from derp.models import SharedFact, SharedFactState
from derp.models.shared_fact import SHARED_FACT_MAX_LENGTH

SHARED_FACT_QUERY_LIMIT = 100


class SharedFactDecisionConflictError(RuntimeError):
    """Raised when a completed review is changed to the opposite decision."""


def _scope_condition(thread_id: int | None) -> ColumnElement[bool]:
    return (
        SharedFact.thread_id.is_(None)
        if thread_id is None
        else SharedFact.thread_id == thread_id
    )


def _validate_scope(thread_id: int | None) -> None:
    if thread_id is not None and thread_id <= 0:
        raise ValueError("Thread ID must be positive")


def _normalize_fact_text(fact_text: str) -> str:
    normalized = fact_text.strip()
    if not normalized:
        raise ValueError("Shared fact must not be blank")
    if len(normalized) > SHARED_FACT_MAX_LENGTH:
        raise ValueError(
            f"Shared fact must be at most {SHARED_FACT_MAX_LENGTH} characters"
        )
    return normalized


async def propose_shared_fact(
    session: AsyncSession,
    *,
    chat_id: UUID,
    thread_id: int | None,
    proposer_user_id: UUID,
    fact_text: str,
) -> SharedFact:
    """Create an untrusted fact proposal in one exact chat/topic scope."""
    _validate_scope(thread_id)
    proposal = SharedFact(
        chat_id=chat_id,
        thread_id=thread_id,
        proposed_by_user_id=proposer_user_id,
        fact_text=_normalize_fact_text(fact_text),
        state=SharedFactState.PROPOSED.value,
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def list_approved_shared_facts(
    session: AsyncSession,
    *,
    chat_id: UUID,
    thread_id: int | None,
    limit: int = SHARED_FACT_QUERY_LIMIT,
) -> list[SharedFact]:
    """Return approved facts only, in stable proposal chronology."""
    _validate_scope(thread_id)
    if not 1 <= limit <= SHARED_FACT_QUERY_LIMIT:
        raise ValueError(
            f"Shared fact limit must be between 1 and {SHARED_FACT_QUERY_LIMIT}"
        )
    result = await session.execute(
        select(SharedFact)
        .where(
            SharedFact.chat_id == chat_id,
            _scope_condition(thread_id),
            SharedFact.state == SharedFactState.APPROVED.value,
        )
        .order_by(SharedFact.created_at, SharedFact.id)
        .limit(limit)
    )
    return list(result.scalars())


async def _decide_shared_fact(
    session: AsyncSession,
    *,
    fact_id: UUID,
    chat_id: UUID,
    thread_id: int | None,
    admin_actor_id: UUID,
    decision: SharedFactState,
    decided_at: datetime | None,
) -> SharedFact:
    _validate_scope(thread_id)
    if decision not in {SharedFactState.APPROVED, SharedFactState.REJECTED}:
        raise ValueError("A review must approve or reject a shared fact")
    timestamp = decided_at or datetime.now(UTC)
    result = await session.execute(
        update(SharedFact)
        .where(
            SharedFact.id == fact_id,
            SharedFact.chat_id == chat_id,
            _scope_condition(thread_id),
            SharedFact.state == SharedFactState.PROPOSED.value,
        )
        .values(
            state=decision.value,
            decided_by_user_id=admin_actor_id,
            decided_at=timestamp,
            updated_at=timestamp,
        )
        .returning(SharedFact)
    )
    if reviewed := result.scalar_one_or_none():
        return reviewed

    current = await session.scalar(
        select(SharedFact).where(
            SharedFact.id == fact_id,
            SharedFact.chat_id == chat_id,
            _scope_condition(thread_id),
        )
    )
    if current is None:
        raise LookupError("Shared fact does not exist in this chat/topic scope")
    if current.state == decision.value:
        return current
    raise SharedFactDecisionConflictError(
        f"Shared fact was already {current.state}; cannot mark it {decision.value}"
    )


async def approve_shared_fact(
    session: AsyncSession,
    *,
    fact_id: UUID,
    chat_id: UUID,
    thread_id: int | None,
    admin_actor_id: UUID,
    decided_at: datetime | None = None,
) -> SharedFact:
    """Approve a proposal once; retries preserve its original review audit."""
    return await _decide_shared_fact(
        session,
        fact_id=fact_id,
        chat_id=chat_id,
        thread_id=thread_id,
        admin_actor_id=admin_actor_id,
        decision=SharedFactState.APPROVED,
        decided_at=decided_at,
    )


async def reject_shared_fact(
    session: AsyncSession,
    *,
    fact_id: UUID,
    chat_id: UUID,
    thread_id: int | None,
    admin_actor_id: UUID,
    decided_at: datetime | None = None,
) -> SharedFact:
    """Reject a proposal once; retries preserve its original review audit."""
    return await _decide_shared_fact(
        session,
        fact_id=fact_id,
        chat_id=chat_id,
        thread_id=thread_id,
        admin_actor_id=admin_actor_id,
        decision=SharedFactState.REJECTED,
        decided_at=decided_at,
    )


async def forget_approved_shared_facts(
    session: AsyncSession,
    *,
    chat_id: UUID,
    thread_id: int | None,
    all_threads: bool = False,
) -> int:
    """Delete approved facts in one scope without touching conversation history."""
    _validate_scope(thread_id)
    if all_threads and thread_id is not None:
        raise ValueError("all_threads cannot be combined with a thread ID")
    result = await session.execute(
        delete(SharedFact).where(
            SharedFact.chat_id == chat_id,
            true() if all_threads else _scope_condition(thread_id),
            SharedFact.state == SharedFactState.APPROVED.value,
        )
    )
    return result.rowcount or 0


__all__ = [
    "SHARED_FACT_QUERY_LIMIT",
    "SharedFactDecisionConflictError",
    "approve_shared_fact",
    "forget_approved_shared_facts",
    "list_approved_shared_facts",
    "propose_shared_fact",
    "reject_shared_fact",
]
