"""Content-safe domain values for deferred paid-tool approvals."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolResults,
    ModelMessage,
    ToolCallPart,
)

from derp.operations import OperationId, QuoteId


class DeferredToolStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    RESUMED = "resumed"


class ApprovalChoice(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class DecisionDisposition(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    EXPIRED = "expired"


class ResumeUnavailableReason(StrEnum):
    PENDING = "pending"
    DENIED = "denied"
    EXPIRED = "expired"
    RESUMED = "resumed"
    LEASED = "leased"


@dataclass(frozen=True, slots=True)
class ApprovalCapability:
    """Presented token plus the exact Telegram callback principal and scope."""

    token: str = field(repr=False)
    requester_telegram_id: int
    chat_telegram_id: int
    thread_id: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("approval token must not be blank")
        if len(self.token) > 128:
            raise ValueError("approval token is too long")
        if (
            isinstance(self.requester_telegram_id, bool)
            or not isinstance(self.requester_telegram_id, int)
            or self.requester_telegram_id <= 0
        ):
            raise ValueError("requester_telegram_id must be positive")
        if (
            isinstance(self.chat_telegram_id, bool)
            or not isinstance(self.chat_telegram_id, int)
            or self.chat_telegram_id == 0
        ):
            raise ValueError("chat_telegram_id must not be zero")
        if self.thread_id is not None and (
            isinstance(self.thread_id, bool)
            or not isinstance(self.thread_id, int)
            or self.thread_id <= 0
        ):
            raise ValueError("thread_id must be positive")


@dataclass(frozen=True, slots=True)
class DeferredToolSnapshot:
    """Content-free durable state safe to attach to logs and decisions."""

    request_id: uuid.UUID
    operation_id: OperationId
    quote_id: QuoteId
    requester_id: uuid.UUID
    requester_telegram_id: int
    chat_id: uuid.UUID
    chat_telegram_id: int
    thread_id: int | None
    message_id: int
    tool_name: str
    tool_call_id: str
    status: DeferredToolStatus
    expires_at: datetime
    decided_at: datetime | None
    resumed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, uuid.UUID):
            raise TypeError("request_id must be a UUID")
        if not isinstance(self.operation_id, OperationId):
            raise TypeError("operation_id must be an OperationId")
        if not isinstance(self.quote_id, QuoteId):
            raise TypeError("quote_id must be a QuoteId")
        for name in ("requester_id", "chat_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if self.requester_telegram_id <= 0:
            raise ValueError("requester_telegram_id must be positive")
        if self.chat_telegram_id == 0:
            raise ValueError("chat_telegram_id must not be zero")
        if self.thread_id is not None and self.thread_id <= 0:
            raise ValueError("thread_id must be positive")
        if self.message_id <= 0:
            raise ValueError("message_id must be positive")
        if not self.tool_name.strip() or not self.tool_call_id.strip():
            raise ValueError("tool identity must not be blank")
        for name in ("expires_at", "created_at", "updated_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        for name in ("decided_at", "resumed_at"):
            value = getattr(self, name)
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("expiry must follow creation")


@dataclass(frozen=True, slots=True)
class DeferredToolHandle:
    """New or idempotently recovered request and its opaque capability."""

    snapshot: DeferredToolSnapshot
    callback_token: str = field(repr=False)
    created: bool

    def __post_init__(self) -> None:
        if not self.callback_token:
            raise ValueError("callback_token must not be blank")


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Result of an authenticated approve or deny transition."""

    requested: ApprovalChoice
    disposition: DecisionDisposition
    snapshot: DeferredToolSnapshot

    @property
    def applied(self) -> bool:
        return self.disposition is DecisionDisposition.APPLIED


@dataclass(frozen=True, slots=True)
class DeferredResumeInput:
    """Server-reconstructed arguments for the follow-up agent run."""

    message_history: tuple[ModelMessage, ...] = field(repr=False)
    deferred_tool_results: DeferredToolResults = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResumeLease:
    """Exclusive bounded authority to resume one approved agent run."""

    snapshot: DeferredToolSnapshot
    claimed_at: datetime
    lease_expires_at: datetime
    _validated_arguments: dict[str, object] = field(repr=False)
    _original_history: tuple[ModelMessage, ...] = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("claimed_at", "lease_expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("lease expiry must follow its claim")
        if self.snapshot.status is not DeferredToolStatus.APPROVED:
            raise ValueError("resume lease requires an approved request")
        if not self._original_history:
            raise ValueError("resume lease history must not be empty")

    def build_run_input(self) -> DeferredResumeInput:
        """Build approval results only from the persisted original request."""
        call = ToolCallPart(
            tool_name=self.snapshot.tool_name,
            args=deepcopy(self._validated_arguments),
            tool_call_id=self.snapshot.tool_call_id,
        )
        requests = DeferredToolRequests(approvals=[call])
        return DeferredResumeInput(
            message_history=deepcopy(self._original_history),
            deferred_tool_results=requests.build_results(approve_all=True),
        )


@dataclass(frozen=True, slots=True)
class ResumeUnavailable:
    """Typed non-claim result for pending, final, or actively leased work."""

    reason: ResumeUnavailableReason
    snapshot: DeferredToolSnapshot
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.retry_at is not None and (
            self.retry_at.tzinfo is None or self.retry_at.utcoffset() is None
        ):
            raise ValueError("retry_at must be timezone-aware")


type ResumeClaim = ResumeLease | ResumeUnavailable


@dataclass(frozen=True, slots=True)
class ExpirationSweep:
    """One bounded expiration pass without persisted message content."""

    request_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(value, uuid.UUID) for value in self.request_ids):
            raise TypeError("expiration IDs must be UUID values")

    @property
    def expired_count(self) -> int:
        return len(self.request_ids)


__all__ = [
    "ApprovalCapability",
    "ApprovalChoice",
    "ApprovalDecision",
    "DecisionDisposition",
    "DeferredResumeInput",
    "DeferredToolHandle",
    "DeferredToolSnapshot",
    "DeferredToolStatus",
    "ExpirationSweep",
    "ResumeClaim",
    "ResumeLease",
    "ResumeUnavailable",
    "ResumeUnavailableReason",
]
