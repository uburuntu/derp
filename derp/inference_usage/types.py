"""Provider-neutral contracts for content-free inference usage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

MAX_PROVIDER_LENGTH = 64
MAX_PROVIDER_IDENTIFIER_LENGTH = 255
MAX_ACTUAL_COST_USD = Decimal("99999999.999999999999")
ACTUAL_COST_SCALE = 12


class InferenceStatus(StrEnum):
    """Durable provider-attempt outcome."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InferenceOutcome(StrEnum):
    """Terminal outcome accepted when completing an attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CostReconciliationStatus(StrEnum):
    """Whether actual normalized provider cost is ready."""

    NOT_READY = "not_ready"
    PENDING = "pending"
    RECONCILED = "reconciled"
    UNAVAILABLE = "unavailable"


class InferenceUsageError(RuntimeError):
    """Base error for inference usage persistence invariants."""


class InferenceUsageNotFoundError(InferenceUsageError):
    """The requested inference attempt does not exist."""


class InferenceUsageConflictError(InferenceUsageError):
    """An idempotency identity was reused for different immutable facts."""


class InvalidInferenceUsageTransitionError(InferenceUsageError):
    """A requested state transition is not legal for the stored attempt."""


class InferenceReconciliationClaimLostError(InferenceUsageError):
    """A reconciliation worker no longer owns the durable lease."""


@dataclass(frozen=True, slots=True)
class InferenceUsageId:
    """Caller-generated idempotency identity for one provider attempt."""

    value: uuid.UUID

    def __post_init__(self) -> None:
        _require_uuid("inference usage ID", self.value)

    @classmethod
    def new(cls) -> Self:
        """Create a new opaque attempt identity before provider I/O."""
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class InferenceTokenUsage:
    """Provider-reported token categories without prompt or response content."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cache_read_tokens", self.cache_read_tokens),
            ("cache_write_tokens", self.cache_write_tokens),
            ("reasoning_tokens", self.reasoning_tokens),
            ("audio_input_tokens", self.audio_input_tokens),
            ("audio_output_tokens", self.audio_output_tokens),
        ):
            _require_non_negative_int(name, value)
        if self.total_tokens is not None:
            _require_non_negative_int("total_tokens", self.total_tokens)


@dataclass(frozen=True, slots=True)
class InferenceUsageStart:
    """Content-free identity persisted before one provider call."""

    id: InferenceUsageId
    operation_id: uuid.UUID | None
    user_id: uuid.UUID
    chat_id: uuid.UUID | None
    provider: str
    provider_model_id: str
    started_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, InferenceUsageId):
            raise TypeError("id must be an InferenceUsageId")
        _require_optional_uuid("operation_id", self.operation_id)
        _require_uuid("user_id", self.user_id)
        _require_optional_uuid("chat_id", self.chat_id)
        _require_identifier("provider", self.provider, MAX_PROVIDER_LENGTH)
        _require_identifier(
            "provider_model_id",
            self.provider_model_id,
            MAX_PROVIDER_IDENTIFIER_LENGTH,
        )
        _require_aware_datetime("started_at", self.started_at)


@dataclass(frozen=True, slots=True)
class InferenceUsageCompletion:
    """Terminal provider outcome and optional reported usage identifiers."""

    outcome: InferenceOutcome
    completed_at: datetime
    tokens: InferenceTokenUsage | None = None
    provider_response_id: str | None = None
    provider_generation_id: str | None = None
    actual_model_id: str | None = None
    downstream_provider: str | None = None
    actual_cost_usd: Decimal | None = None
    route_policy_matched: bool = True
    cost_reconciliation_status: CostReconciliationStatus = (
        CostReconciliationStatus.PENDING
    )

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, InferenceOutcome):
            raise TypeError("outcome must be an InferenceOutcome")
        _require_aware_datetime("completed_at", self.completed_at)
        if self.tokens is not None and not isinstance(self.tokens, InferenceTokenUsage):
            raise TypeError("tokens must be InferenceTokenUsage or None")
        _require_optional_identifier(
            "provider_response_id",
            self.provider_response_id,
            MAX_PROVIDER_IDENTIFIER_LENGTH,
        )
        if not isinstance(self.route_policy_matched, bool):
            raise TypeError("route_policy_matched must be a bool")
        if self.actual_cost_usd is not None:
            _require_actual_cost(self.actual_cost_usd)
        if self.cost_reconciliation_status is CostReconciliationStatus.RECONCILED:
            if self.actual_cost_usd is None:
                raise ValueError("reconciled completion requires actual_cost_usd")
        elif self.cost_reconciliation_status not in {
            CostReconciliationStatus.PENDING,
            CostReconciliationStatus.UNAVAILABLE,
        }:
            raise ValueError("completed usage cost has an invalid state")
        elif self.actual_cost_usd is not None:
            raise ValueError("known actual cost must be atomically reconciled")
        _require_optional_identifier(
            "provider_generation_id",
            self.provider_generation_id,
            MAX_PROVIDER_IDENTIFIER_LENGTH,
        )
        _require_optional_identifier(
            "actual_model_id",
            self.actual_model_id,
            MAX_PROVIDER_IDENTIFIER_LENGTH,
        )
        _require_optional_identifier(
            "downstream_provider",
            self.downstream_provider,
            MAX_PROVIDER_IDENTIFIER_LENGTH,
        )


@dataclass(frozen=True, slots=True)
class InferenceCostReconciliation:
    """Final normalized actual cost for a completed provider attempt."""

    actual_cost_usd: Decimal
    reconciled_at: datetime

    def __post_init__(self) -> None:
        _require_actual_cost(self.actual_cost_usd)
        _require_aware_datetime("reconciled_at", self.reconciled_at)


@dataclass(frozen=True, slots=True)
class InferenceCostReconciliationClaim:
    """Exclusive, expiring permission to fetch one record's provider cost."""

    usage_id: InferenceUsageId
    token: uuid.UUID
    provider_generation_id: str | None
    expected_tokens: InferenceTokenUsage | None
    claimed_at: datetime
    lease_expires_at: datetime
    attempt: int

    def __post_init__(self) -> None:
        if not isinstance(self.usage_id, InferenceUsageId):
            raise TypeError("usage_id must be an InferenceUsageId")
        _require_uuid("token", self.token)
        _require_optional_identifier(
            "provider_generation_id",
            self.provider_generation_id,
            MAX_PROVIDER_IDENTIFIER_LENGTH,
        )
        if self.expected_tokens is not None and not isinstance(
            self.expected_tokens, InferenceTokenUsage
        ):
            raise TypeError("expected_tokens must be InferenceTokenUsage or None")
        _require_aware_datetime("claimed_at", self.claimed_at)
        _require_aware_datetime("lease_expires_at", self.lease_expires_at)
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("lease_expires_at must follow claimed_at")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError("attempt must be an integer")
        if self.attempt <= 0:
            raise ValueError("attempt must be positive")


@dataclass(frozen=True, slots=True)
class InferenceUsageSnapshot:
    """Complete content-free state returned by the repository."""

    id: InferenceUsageId
    operation_id: uuid.UUID | None
    user_id: uuid.UUID
    chat_id: uuid.UUID | None
    provider: str
    provider_model_id: str
    provider_response_id: str | None
    provider_generation_id: str | None
    actual_model_id: str | None
    downstream_provider: str | None
    route_policy_matched: bool
    tokens: InferenceTokenUsage | None
    actual_cost_usd: Decimal | None
    status: InferenceStatus
    reconciliation_status: CostReconciliationStatus
    provider_started_at: datetime
    provider_completed_at: datetime | None
    usage_recorded_at: datetime | None
    reconciliation_requested_at: datetime | None
    reconciled_at: datetime | None
    reconciliation_retry_at: datetime | None = None
    reconciliation_claim_token: uuid.UUID | None = None
    reconciliation_claimed_at: datetime | None = None
    reconciliation_lease_expires_at: datetime | None = None
    reconciliation_attempts: int = 0


def _require_uuid(name: str, value: object) -> None:
    if not isinstance(value, uuid.UUID):
        raise TypeError(f"{name} must be a UUID")


def _require_optional_uuid(name: str, value: object) -> None:
    if value is not None:
        _require_uuid(name, value)


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_identifier(name: str, value: object, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")


def _require_optional_identifier(
    name: str,
    value: object,
    max_length: int,
) -> None:
    if value is not None:
        _require_identifier(name, value, max_length)


def _require_aware_datetime(name: str, value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


def _require_actual_cost(value: object) -> None:
    if not isinstance(value, Decimal):
        raise TypeError("actual_cost_usd must be a Decimal")
    if not value.is_finite() or value < 0 or value > MAX_ACTUAL_COST_USD:
        raise ValueError("actual_cost_usd must be finite, non-negative, and bounded")
    normalized = value.normalize() if value else Decimal(0)
    if normalized.as_tuple().exponent < -ACTUAL_COST_SCALE:
        raise ValueError(
            f"actual_cost_usd supports at most {ACTUAL_COST_SCALE} decimal places"
        )


__all__ = [
    "ACTUAL_COST_SCALE",
    "MAX_ACTUAL_COST_USD",
    "MAX_PROVIDER_IDENTIFIER_LENGTH",
    "MAX_PROVIDER_LENGTH",
    "CostReconciliationStatus",
    "InferenceCostReconciliation",
    "InferenceCostReconciliationClaim",
    "InferenceOutcome",
    "InferenceStatus",
    "InferenceTokenUsage",
    "InferenceUsageCompletion",
    "InferenceUsageConflictError",
    "InferenceUsageError",
    "InferenceUsageId",
    "InferenceUsageNotFoundError",
    "InferenceReconciliationClaimLostError",
    "InferenceUsageSnapshot",
    "InferenceUsageStart",
    "InvalidInferenceUsageTransitionError",
]
