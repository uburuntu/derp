"""Types for credit operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from derp.catalog import GoogleModelSpec
from derp.execution import ExecutionPlan


@dataclass(frozen=True, slots=True)
class CreditCheckResult:
    """Result of checking credit availability for a tool/feature.

    Used both for pre-check (can user access this?) and for tracking
    what to deduct after successful execution.
    """

    allowed: bool
    plan: ExecutionPlan | None
    source: Literal["free", "chat", "user", "rejected"]
    credits_to_deduct: int
    credits_remaining: int | None  # None for free tier
    free_remaining: int | None  # Remaining free uses today
    reject_reason: str | None = None

    @property
    def model(self) -> GoogleModelSpec | None:
        """Exact model selected by the execution plan, when provider-backed."""
        return self.plan.model if self.plan else None

    @property
    def model_id(self) -> str | None:
        """Concrete provider ID retained for persistence compatibility."""
        return self.model.provider_model_id if self.model else None

    def require_plan(self) -> ExecutionPlan:
        """Return the plan for a provider-backed feature."""
        if self.plan is None:
            raise RuntimeError("Provider-backed feature resolved without a plan")
        return self.plan

    @property
    def is_free_use(self) -> bool:
        """Whether this is a free tier use (no credits deducted)."""
        return self.source == "free"

    @property
    def is_paid(self) -> bool:
        """Whether this uses paid credits (chat or user)."""
        return self.source in ("chat", "user")
