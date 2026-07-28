"""Durable, content-free in-bot support."""

from derp.support.service import SupportRequestService, TermsAcceptanceService
from derp.support.types import (
    OpenSupportResult,
    OperatorSupportCase,
    ResolveSupportResult,
    SupportCapacityError,
    SupportCase,
    SupportKind,
    SupportSource,
    SupportStatus,
)

__all__ = [
    "OpenSupportResult",
    "OperatorSupportCase",
    "ResolveSupportResult",
    "SupportCapacityError",
    "SupportCase",
    "SupportKind",
    "SupportRequestService",
    "SupportSource",
    "SupportStatus",
    "TermsAcceptanceService",
]
