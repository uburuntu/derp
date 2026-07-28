"""Small immutable values for the content-free support workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class SupportCase:
    """Identifier-free support case safe to render to either actor."""

    reference: str
    kind: SupportKind
    status: SupportStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OpenSupportResult:
    case: SupportCase
    created: bool


@dataclass(frozen=True, slots=True)
class OperatorSupportCase:
    """Bounded case projection visible only inside the operator console."""

    reference: str
    kind: SupportKind
    created_at: datetime
    requester_telegram_id: int


@dataclass(frozen=True, slots=True)
class ResolveSupportResult:
    """The durable resolution outcome and its requester notification target."""

    case: OperatorSupportCase
    resolved_at: datetime
    changed: bool


class SupportCapacityError(RuntimeError):
    """The actor already has the maximum number of open case categories."""
