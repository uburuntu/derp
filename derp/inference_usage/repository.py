"""Transactional persistence for content-free provider inference usage."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.inference_usage.types import (
    CostReconciliationStatus,
    InferenceCostReconciliation,
    InferenceCostReconciliationClaim,
    InferenceReconciliationClaimLostError,
    InferenceStatus,
    InferenceTokenUsage,
    InferenceUsageCompletion,
    InferenceUsageConflictError,
    InferenceUsageId,
    InferenceUsageNotFoundError,
    InferenceUsageSnapshot,
    InferenceUsageStart,
    InvalidInferenceUsageTransitionError,
)
from derp.models.inference_usage import InferenceUsage

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

MAX_RECONCILIATION_CLAIM_BATCH = 100
MAX_RECONCILIATION_LEASE = timedelta(hours=1)


class Clock(Protocol):
    """Injectable UTC clock for deterministic persistence timestamps."""

    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InferenceUsageRepository:
    """Own idempotent, row-locked inference usage state transitions."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._clock = clock

    async def register(self, start: InferenceUsageStart) -> InferenceUsageSnapshot:
        """Persist an attempt before provider I/O or return an equivalent retry."""
        values = {
            "id": start.id.value,
            "operation_id": start.operation_id,
            "user_id": start.user_id,
            "chat_id": start.chat_id,
            "provider": start.provider,
            "provider_model_id": start.provider_model_id,
            "provider_response_id": None,
            "provider_generation_id": None,
            **_token_values(None),
            "usage_available": False,
            "actual_cost_usd": None,
            "status": InferenceStatus.PENDING.value,
            "reconciliation_status": CostReconciliationStatus.NOT_READY.value,
            "provider_started_at": start.started_at,
            "provider_completed_at": None,
            "usage_recorded_at": None,
            "reconciliation_requested_at": None,
            "reconciliation_retry_at": None,
            "reconciliation_claim_token": None,
            "reconciliation_claimed_at": None,
            "reconciliation_lease_expires_at": None,
            "reconciliation_attempts": 0,
            "reconciled_at": None,
        }
        async with self._transactions() as session:
            await session.scalar(
                insert(InferenceUsage)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[InferenceUsage.id])
                .returning(InferenceUsage.id)
            )
            stored = await session.get(InferenceUsage, start.id.value)
            if stored is None:
                raise InferenceUsageNotFoundError(
                    f"Inference usage {start.id} disappeared during registration"
                )
            if not _start_matches(stored, start):
                raise InferenceUsageConflictError(
                    f"Conflicting inference usage identity {start.id}"
                )
            return _snapshot(stored)

    async def complete(
        self,
        usage_id: InferenceUsageId,
        completion: InferenceUsageCompletion,
    ) -> InferenceUsageSnapshot:
        """Record the terminal provider outcome and queue actual-cost work."""
        async with self._transactions() as session:
            stored = await _load_locked(session, usage_id)
            if stored.status != InferenceStatus.PENDING.value:
                if _completion_matches(stored, completion):
                    return _snapshot(stored)
                raise InvalidInferenceUsageTransitionError(
                    f"Inference usage {usage_id} is already {stored.status}"
                )
            if completion.completed_at < stored.provider_started_at:
                raise InvalidInferenceUsageTransitionError(
                    "Provider completion cannot precede attempt start"
                )

            recorded_at = self._clock()
            if (
                recorded_at.tzinfo is None
                or recorded_at.utcoffset() is None
                or recorded_at < stored.provider_started_at
            ):
                raise InvalidInferenceUsageTransitionError(
                    "Usage recording time cannot precede attempt start"
                )
            stored.status = completion.outcome.value
            stored.provider_completed_at = completion.completed_at
            stored.provider_response_id = completion.provider_response_id
            stored.provider_generation_id = completion.provider_generation_id
            _apply_tokens(stored, completion.tokens)
            stored.usage_available = completion.tokens is not None
            stored.usage_recorded_at = recorded_at
            stored.reconciliation_status = completion.cost_reconciliation_status.value
            stored.reconciliation_requested_at = (
                recorded_at
                if completion.cost_reconciliation_status
                is CostReconciliationStatus.PENDING
                else None
            )
            stored.reconciliation_retry_at = None
            _clear_reconciliation_claim(stored)
            return _snapshot(stored)

    async def reconcile_cost(
        self,
        usage_id: InferenceUsageId,
        reconciliation: InferenceCostReconciliation,
    ) -> InferenceUsageSnapshot:
        """Persist final actual USD cost exactly once for a completed attempt."""
        async with self._transactions() as session:
            stored = await _load_locked(session, usage_id)
            status = CostReconciliationStatus(stored.reconciliation_status)
            if status is CostReconciliationStatus.RECONCILED:
                if stored.actual_cost_usd == reconciliation.actual_cost_usd:
                    return _snapshot(stored)
                raise InferenceUsageConflictError(
                    f"Conflicting actual cost for inference usage {usage_id}"
                )
            if status is not CostReconciliationStatus.PENDING:
                raise InvalidInferenceUsageTransitionError(
                    f"Inference usage {usage_id} is not ready for reconciliation"
                )
            _apply_reconciliation(stored, reconciliation)
            return _snapshot(stored)

    async def claim_pending_reconciliation(
        self,
        *,
        provider: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 20,
    ) -> tuple[InferenceCostReconciliationClaim, ...]:
        """Exclusively lease a bounded, ready batch without waiting on workers."""
        _require_identifier(provider, "provider")
        _require_aware_datetime(now, "now")
        if (
            not isinstance(lease_duration, timedelta)
            or lease_duration <= timedelta(0)
            or lease_duration > MAX_RECONCILIATION_LEASE
        ):
            raise ValueError("lease_duration must be between 0 and 1 hour")
        _require_claim_limit(limit)

        ready_at = func.coalesce(
            InferenceUsage.reconciliation_retry_at,
            InferenceUsage.reconciliation_requested_at,
        )
        statement = (
            select(InferenceUsage)
            .where(
                InferenceUsage.provider == provider,
                InferenceUsage.reconciliation_status
                == CostReconciliationStatus.PENDING.value,
                InferenceUsage.provider_generation_id.is_not(None),
                ready_at <= now,
                or_(
                    InferenceUsage.reconciliation_claim_token.is_(None),
                    InferenceUsage.reconciliation_lease_expires_at <= now,
                ),
            )
            .order_by(ready_at, InferenceUsage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        lease_expires_at = now + lease_duration
        async with self._transactions() as session:
            rows = (await session.scalars(statement)).all()
            claims: list[InferenceCostReconciliationClaim] = []
            for stored in rows:
                stored.reconciliation_claim_token = uuid.uuid4()
                stored.reconciliation_claimed_at = now
                stored.reconciliation_lease_expires_at = lease_expires_at
                stored.reconciliation_retry_at = None
                stored.reconciliation_attempts += 1
                claims.append(_reconciliation_claim(stored))
            return tuple(claims)

    async def reconcile_claimed_cost(
        self,
        claim: InferenceCostReconciliationClaim,
        reconciliation: InferenceCostReconciliation,
    ) -> InferenceUsageSnapshot:
        """Record exact cost only while the caller owns an unexpired lease."""
        _require_claim(claim)
        async with self._transactions() as session:
            stored = await _load_locked(session, claim.usage_id)
            status = CostReconciliationStatus(stored.reconciliation_status)
            if status is CostReconciliationStatus.RECONCILED:
                if stored.actual_cost_usd == reconciliation.actual_cost_usd:
                    return _snapshot(stored)
                raise InferenceUsageConflictError(
                    "reconciled inference cost does not match the claimed result"
                )
            _require_claim_owner(
                stored,
                claim,
                observed_at=reconciliation.reconciled_at,
            )
            _apply_reconciliation(stored, reconciliation)
            return _snapshot(stored)

    async def mark_claimed_cost_unavailable(
        self,
        claim: InferenceCostReconciliationClaim,
        *,
        unavailable_at: datetime,
    ) -> InferenceUsageSnapshot:
        """Terminally close cost work while the caller owns its live lease."""
        _require_claim(claim)
        _require_aware_datetime(unavailable_at, "unavailable_at")
        async with self._transactions() as session:
            stored = await _load_locked(session, claim.usage_id)
            status = CostReconciliationStatus(stored.reconciliation_status)
            if status is CostReconciliationStatus.UNAVAILABLE:
                return _snapshot(stored)
            if status is CostReconciliationStatus.RECONCILED:
                return _snapshot(stored)
            _require_claim_owner(stored, claim, observed_at=unavailable_at)
            stored.reconciliation_status = CostReconciliationStatus.UNAVAILABLE.value
            stored.reconciliation_retry_at = None
            _clear_reconciliation_claim(stored)
            return _snapshot(stored)

    async def defer_cost_reconciliation(
        self,
        claim: InferenceCostReconciliationClaim,
        *,
        retry_at: datetime,
        deferred_at: datetime | None = None,
    ) -> InferenceUsageSnapshot:
        """Release an owned lease and make the pending record retryable later."""
        _require_claim(claim)
        observed_at = deferred_at or self._clock()
        _require_aware_datetime(observed_at, "deferred_at")
        _require_aware_datetime(retry_at, "retry_at")
        if retry_at <= observed_at:
            raise ValueError("retry_at must follow deferred_at")
        async with self._transactions() as session:
            stored = await _load_locked(session, claim.usage_id)
            if (
                CostReconciliationStatus(stored.reconciliation_status)
                is CostReconciliationStatus.RECONCILED
            ):
                return _snapshot(stored)
            _require_claim_owner(stored, claim, observed_at=observed_at)
            _clear_reconciliation_claim(stored)
            stored.reconciliation_retry_at = retry_at
            return _snapshot(stored)

    async def fail_stale_attempts(
        self,
        *,
        provider: str,
        started_before: datetime,
        failed_at: datetime,
        limit: int = 20,
    ) -> int:
        """Terminally close a bounded batch abandoned before provider completion."""
        _require_identifier(provider, "provider")
        _require_aware_datetime(started_before, "started_before")
        _require_aware_datetime(failed_at, "failed_at")
        if started_before > failed_at:
            raise ValueError("started_before cannot follow failed_at")
        _require_claim_limit(limit)
        statement = (
            select(InferenceUsage)
            .where(
                InferenceUsage.provider == provider,
                InferenceUsage.status == InferenceStatus.PENDING.value,
                InferenceUsage.provider_started_at <= started_before,
            )
            .order_by(InferenceUsage.provider_started_at, InferenceUsage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        async with self._transactions() as session:
            rows = (await session.scalars(statement)).all()
            for stored in rows:
                stored.status = InferenceStatus.FAILED.value
                stored.provider_completed_at = failed_at
                stored.usage_recorded_at = failed_at
                stored.reconciliation_status = (
                    CostReconciliationStatus.UNAVAILABLE.value
                )
            return len(rows)

    async def get(self, usage_id: InferenceUsageId) -> InferenceUsageSnapshot:
        """Return one persisted attempt without locking it."""
        async with self._transactions() as session:
            stored = await session.get(InferenceUsage, usage_id.value)
            if stored is None:
                raise InferenceUsageNotFoundError(f"Unknown inference usage {usage_id}")
            return _snapshot(stored)

    async def list_pending_reconciliation(
        self,
        *,
        requested_before: datetime,
        limit: int = 100,
    ) -> tuple[InferenceUsageSnapshot, ...]:
        """List a bounded, deterministic batch awaiting actual-cost resolution."""
        if requested_before.tzinfo is None or requested_before.utcoffset() is None:
            raise ValueError("requested_before must be timezone-aware")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")

        statement = (
            select(InferenceUsage)
            .where(
                InferenceUsage.reconciliation_status
                == CostReconciliationStatus.PENDING.value,
                InferenceUsage.reconciliation_requested_at <= requested_before,
            )
            .order_by(
                InferenceUsage.reconciliation_requested_at,
                InferenceUsage.id,
            )
            .limit(limit)
        )
        async with self._transactions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_snapshot(row) for row in rows)


async def _load_locked(
    session: AsyncSession,
    usage_id: InferenceUsageId,
) -> InferenceUsage:
    stored = await session.get(
        InferenceUsage,
        usage_id.value,
        with_for_update=True,
    )
    if stored is None:
        raise InferenceUsageNotFoundError(f"Unknown inference usage {usage_id}")
    return stored


def _reconciliation_claim(
    stored: InferenceUsage,
) -> InferenceCostReconciliationClaim:
    if (
        stored.reconciliation_claim_token is None
        or stored.reconciliation_claimed_at is None
        or stored.reconciliation_lease_expires_at is None
    ):
        raise RuntimeError("claimed reconciliation omitted its lease state")
    return InferenceCostReconciliationClaim(
        usage_id=InferenceUsageId(stored.id),
        token=stored.reconciliation_claim_token,
        provider_generation_id=stored.provider_generation_id,
        expected_tokens=_stored_tokens(stored),
        claimed_at=stored.reconciliation_claimed_at,
        lease_expires_at=stored.reconciliation_lease_expires_at,
        attempt=stored.reconciliation_attempts,
    )


def _require_claim_owner(
    stored: InferenceUsage,
    claim: InferenceCostReconciliationClaim,
    *,
    observed_at: datetime,
) -> None:
    if (
        stored.reconciliation_status != CostReconciliationStatus.PENDING.value
        or stored.reconciliation_claim_token != claim.token
        or stored.reconciliation_claimed_at != claim.claimed_at
        or stored.reconciliation_lease_expires_at != claim.lease_expires_at
        or stored.reconciliation_attempts != claim.attempt
        or stored.provider_generation_id != claim.provider_generation_id
        or _stored_tokens(stored) != claim.expected_tokens
        or stored.reconciliation_claimed_at is None
        or stored.reconciliation_lease_expires_at is None
        or observed_at < stored.reconciliation_claimed_at
        or observed_at >= stored.reconciliation_lease_expires_at
    ):
        raise InferenceReconciliationClaimLostError(
            "inference reconciliation lease is no longer owned"
        )


def _apply_reconciliation(
    stored: InferenceUsage,
    reconciliation: InferenceCostReconciliation,
) -> None:
    if (
        stored.reconciliation_requested_at is None
        or reconciliation.reconciled_at < stored.reconciliation_requested_at
    ):
        raise InvalidInferenceUsageTransitionError(
            "Cost reconciliation cannot precede its request"
        )
    stored.actual_cost_usd = reconciliation.actual_cost_usd
    stored.reconciliation_status = CostReconciliationStatus.RECONCILED.value
    stored.reconciled_at = reconciliation.reconciled_at
    stored.reconciliation_retry_at = None
    _clear_reconciliation_claim(stored)


def _clear_reconciliation_claim(stored: InferenceUsage) -> None:
    stored.reconciliation_claim_token = None
    stored.reconciliation_claimed_at = None
    stored.reconciliation_lease_expires_at = None


def _require_claim(claim: object) -> None:
    if not isinstance(claim, InferenceCostReconciliationClaim):
        raise TypeError("claim must be an InferenceCostReconciliationClaim")


def _require_identifier(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a bounded identifier")


def _require_aware_datetime(value: object, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


def _require_claim_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= MAX_RECONCILIATION_CLAIM_BATCH:
        raise ValueError(
            f"limit must be between 1 and {MAX_RECONCILIATION_CLAIM_BATCH}"
        )


def _start_matches(stored: InferenceUsage, start: InferenceUsageStart) -> bool:
    return (
        stored.operation_id == start.operation_id
        and stored.user_id == start.user_id
        and stored.chat_id == start.chat_id
        and stored.provider == start.provider
        and stored.provider_model_id == start.provider_model_id
    )


def _completion_matches(
    stored: InferenceUsage,
    completion: InferenceUsageCompletion,
) -> bool:
    return (
        stored.status == completion.outcome.value
        and stored.provider_response_id == completion.provider_response_id
        and stored.provider_generation_id == completion.provider_generation_id
        and stored.usage_available == (completion.tokens is not None)
        and _stored_tokens(stored) == completion.tokens
        and (
            stored.reconciliation_status == completion.cost_reconciliation_status.value
            or (
                completion.cost_reconciliation_status
                is CostReconciliationStatus.PENDING
                and stored.reconciliation_status
                == CostReconciliationStatus.RECONCILED.value
            )
        )
    )


def _token_values(tokens: InferenceTokenUsage | None) -> dict[str, int | None]:
    if tokens is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": None,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "audio_input_tokens": 0,
            "audio_output_tokens": 0,
        }
    return {
        "input_tokens": tokens.input_tokens,
        "output_tokens": tokens.output_tokens,
        "total_tokens": tokens.total_tokens,
        "cache_read_tokens": tokens.cache_read_tokens,
        "cache_write_tokens": tokens.cache_write_tokens,
        "reasoning_tokens": tokens.reasoning_tokens,
        "audio_input_tokens": tokens.audio_input_tokens,
        "audio_output_tokens": tokens.audio_output_tokens,
    }


def _apply_tokens(
    stored: InferenceUsage,
    tokens: InferenceTokenUsage | None,
) -> None:
    for name, value in _token_values(tokens).items():
        setattr(stored, name, value)


def _stored_tokens(stored: InferenceUsage) -> InferenceTokenUsage | None:
    if not stored.usage_available:
        return None
    return InferenceTokenUsage(
        input_tokens=stored.input_tokens,
        output_tokens=stored.output_tokens,
        total_tokens=stored.total_tokens,
        cache_read_tokens=stored.cache_read_tokens,
        cache_write_tokens=stored.cache_write_tokens,
        reasoning_tokens=stored.reasoning_tokens,
        audio_input_tokens=stored.audio_input_tokens,
        audio_output_tokens=stored.audio_output_tokens,
    )


def _snapshot(stored: InferenceUsage) -> InferenceUsageSnapshot:
    return InferenceUsageSnapshot(
        id=InferenceUsageId(stored.id),
        operation_id=stored.operation_id,
        user_id=stored.user_id,
        chat_id=stored.chat_id,
        provider=stored.provider,
        provider_model_id=stored.provider_model_id,
        provider_response_id=stored.provider_response_id,
        provider_generation_id=stored.provider_generation_id,
        tokens=_stored_tokens(stored),
        actual_cost_usd=stored.actual_cost_usd,
        status=InferenceStatus(stored.status),
        reconciliation_status=CostReconciliationStatus(stored.reconciliation_status),
        provider_started_at=stored.provider_started_at,
        provider_completed_at=stored.provider_completed_at,
        usage_recorded_at=stored.usage_recorded_at,
        reconciliation_requested_at=stored.reconciliation_requested_at,
        reconciled_at=stored.reconciled_at,
        reconciliation_retry_at=stored.reconciliation_retry_at,
        reconciliation_claim_token=stored.reconciliation_claim_token,
        reconciliation_claimed_at=stored.reconciliation_claimed_at,
        reconciliation_lease_expires_at=stored.reconciliation_lease_expires_at,
        reconciliation_attempts=stored.reconciliation_attempts,
    )


__all__ = [
    "MAX_RECONCILIATION_CLAIM_BATCH",
    "MAX_RECONCILIATION_LEASE",
    "InferenceUsageRepository",
    "TransactionFactory",
]
