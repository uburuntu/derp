"""Types for credit operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from derp.catalog import GoogleModelSpec


@dataclass(frozen=True, slots=True)
class CreditCheckResult:
    """Result of checking credit availability for a tool/feature.

    Used both for pre-check (can user access this?) and for tracking
    what to deduct after successful execution.
    """

    allowed: bool
    model: GoogleModelSpec | None
    source: Literal["free", "chat", "user", "rejected"]
    credits_to_deduct: int
    credits_remaining: int | None  # None for free tier
    free_remaining: int | None  # Remaining free uses today
    reject_reason: str | None = None

    @property
    def model_id(self) -> str | None:
        """Concrete provider ID retained for persistence compatibility."""
        return self.model.provider_model_id if self.model else None

    def require_model(self) -> GoogleModelSpec:
        """Return the provider model for a provider-backed feature."""
        if self.model is None:
            raise RuntimeError("Provider-backed feature resolved without a model")
        return self.model

    @property
    def is_free_use(self) -> bool:
        """Whether this is a free tier use (no credits deducted)."""
        return self.source == "free"

    @property
    def is_paid(self) -> bool:
        """Whether this uses paid credits (chat or user)."""
        return self.source in ("chat", "user")
