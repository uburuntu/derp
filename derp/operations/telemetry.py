"""Content-free observability vocabulary for every paid operation."""

from __future__ import annotations

import re

import logfire

from derp.operations.types import FundingAuthorization, Quote

_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")


def record_operation_quote(
    span: logfire.LogfireSpan,
    quote: Quote,
    *,
    provider_model_id: str,
) -> None:
    """Attach fixed-price economics using queryable numeric attributes."""
    if not provider_model_id.strip():
        raise ValueError("provider_model_id must not be blank")
    span.set_attributes(
        {
            "gen_ai.request.model": provider_model_id,
            "derp.operation.capability": quote.key.feature.value,
            "derp.operation.model_key": quote.key.model_key.value,
            "derp.operation.context_band": quote.key.context_band.value,
            "derp.operation.quoted_credits": quote.credits,
            "derp.operation.estimated_provider_cost_usd": float(
                quote.estimated_provider_cost_usd
            ),
            "derp.operation.pricing_version": quote.pricing_version,
        }
    )


def record_operation_outcome(
    span: logfire.LogfireSpan,
    outcome: str,
    *,
    authorization: FundingAuthorization | None = None,
    terminal_outcome: str | None = None,
) -> None:
    """Record controlled state labels without accepting request content."""
    _require_label(outcome, "outcome")
    if terminal_outcome is not None:
        _require_label(terminal_outcome, "terminal_outcome")
    span.set_attributes(
        {
            "derp.operation.authorization": (
                authorization.value if authorization is not None else "none"
            ),
            "derp.operation.outcome": outcome,
            "derp.operation.terminal_outcome": terminal_outcome or "none",
        }
    )


def _require_label(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not _LABEL_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a controlled telemetry label")


__all__ = ["record_operation_outcome", "record_operation_quote"]
