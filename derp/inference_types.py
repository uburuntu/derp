"""Provider-neutral, content-free inference response metadata."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from derp.inference_usage import InferenceTokenUsage


@dataclass(frozen=True, slots=True)
class InferenceReport:
    """Normalized metadata from one provider response, never message content."""

    provider: str
    requested_model: str
    actual_model: str
    downstream_provider: str | None
    provider_response_id: str | None
    generation_id: str | None
    finish_reason: str | None
    tokens: InferenceTokenUsage | None
    actual_cost_usd: Decimal | None

    def __post_init__(self) -> None:
        for name in ("provider", "requested_model", "actual_model"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if self.tokens is not None and not isinstance(self.tokens, InferenceTokenUsage):
            raise TypeError("tokens must be InferenceTokenUsage or None")
        if self.actual_cost_usd is not None and self.actual_cost_usd < 0:
            raise ValueError("actual_cost_usd cannot be negative")


__all__ = ["InferenceReport"]
