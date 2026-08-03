"""Small immutable values for the content-free support workflow."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class SupportKind(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    PRIVACY = "privacy"
    ACCESS = "access"


class SupportSource(StrEnum):
    SUPPORT = "support"
    PAY_SUPPORT = "paysupport"
    PRIVACY = "privacy"


class SupportStatus(StrEnum):
    OPEN = "open"
    REFUND_PENDING = "refund_pending"
    RESOLVED = "resolved"
    DECLINED = "declined"


class SupportIntakeLookupState(StrEnum):
    FOUND = "found"
    EXPIRED = "expired"
    MISSING = "missing"


class SupportRefundReconciler(Protocol):
    """Close support cases from durable payment-receipt state."""

    async def complete_reconciled_refunds(self, *, limit: int = 50) -> int: ...


class SupportMaintenance(SupportRefundReconciler, Protocol):
    """Bounded support lifecycle maintenance used by a live worker."""

    async def purge_closed_content(self, *, limit: int = 50) -> int: ...

    async def purge_expired_intakes(self, *, limit: int = 50) -> int: ...


@dataclass(frozen=True, slots=True)
class SupportPayment:
    """Bounded payment facts suitable for user and operator selectors."""

    receipt_id: uuid.UUID
    stars: int
    credits: int | None
    created_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class SupportStatusMessage:
    """Stable bot message edited when a case changes state."""

    chat_id: int
    message_id: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.chat_id, bool)
            or not isinstance(self.chat_id, int)
            or self.chat_id == 0
        ):
            raise ValueError("chat_id must be a nonzero integer")
        if (
            isinstance(self.message_id, bool)
            or not isinstance(self.message_id, int)
            or self.message_id <= 0
        ):
            raise ValueError("message_id must be a positive integer")


@dataclass(frozen=True, slots=True)
class SupportIntakeDraft:
    """Category and optional payment bound to one ForceReply prompt."""

    kind: SupportKind
    source: SupportSource
    payment_receipt_id: uuid.UUID | None
    status_message: SupportStatusMessage


@dataclass(frozen=True, slots=True)
class SupportIntakeLookup:
    """Prompt lookup outcome that keeps expiration distinct from absence."""

    state: SupportIntakeLookupState
    intake: SupportIntakeDraft | None = None

    def __post_init__(self) -> None:
        if (self.state is SupportIntakeLookupState.FOUND) != (self.intake is not None):
            raise ValueError("only a found support intake lookup can carry a draft")


@dataclass(frozen=True, slots=True)
class SupportCase:
    """Identifier-free support case safe to render to either actor."""

    reference: str
    kind: SupportKind
    status: SupportStatus
    created_at: datetime
    description: str | None = None
    payment: SupportPayment | None = None
    decision_reason: str | None = None
    status_message: SupportStatusMessage | None = None


@dataclass(frozen=True, slots=True)
class OpenSupportResult:
    case: SupportCase
    created: bool
    status_message: SupportStatusMessage | None = None


@dataclass(frozen=True, slots=True)
class OperatorSupportCase:
    """Bounded case projection visible only inside the operator console."""

    reference: str
    kind: SupportKind
    created_at: datetime
    requester_telegram_id: int
    description: str | None = None
    payment: SupportPayment | None = None
    status: SupportStatus = SupportStatus.OPEN
    status_message: SupportStatusMessage | None = None
    requester_language_code: str = "en"
    decision_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OperatorSupportPage:
    """One deterministic queue page and its total size."""

    cases: tuple[OperatorSupportCase, ...]
    offset: int
    total: int


@dataclass(frozen=True, slots=True)
class ResolveSupportResult:
    """The durable resolution outcome and its requester notification target."""

    case: OperatorSupportCase
    resolved_at: datetime
    changed: bool


@dataclass(frozen=True, slots=True)
class SupportDecisionResult:
    """One operator decision and its stable requester message target."""

    case: OperatorSupportCase
    status: SupportStatus
    reason: str
    changed: bool


class SupportCapacityError(RuntimeError):
    """The actor already has the maximum number of open case categories."""


class SupportReceiptError(RuntimeError):
    """A selected payment is unavailable or belongs to another payer."""
