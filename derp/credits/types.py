"""Types for credit operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from derp.credits.models import ModelTier


class TransactionType(StrEnum):
    """Types of credit transactions for audit trail."""

    PURCHASE = "purchase"  # Bought with Telegram Stars
    SPEND = "spend"  # Used for tool/feature
    REFUND = "refund"  # Refunded after failed operation or dispute
    GIFT = "gift"  # Gifted from another user
    BONUS = "bonus"  # Promotional credits
    EXPIRE = "expire"  # Credits expired (if we add expiry)


@dataclass(frozen=True, slots=True)
class CreditCheckResult:
    """Result of checking credit availability for a tool/feature.

    Used both for pre-check (can user access this?) and for tracking
    what to deduct after successful execution.
    """

    allowed: bool
    tier: ModelTier
    model_id: str
    source: Literal["free", "chat", "user", "rejected"]
    credits_to_deduct: int
    credits_remaining: int | None  # None for free tier
    free_remaining: int | None  # Remaining free uses today
    reject_reason: str | None = None

    @property
    def is_free_use(self) -> bool:
        """Whether this is a free tier use (no credits deducted)."""
        return self.source == "free"

    @property
    def is_paid(self) -> bool:
        """Whether this uses paid credits (chat or user)."""
        return self.source in ("chat", "user")
