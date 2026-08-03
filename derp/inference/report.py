"""Content-free provider response reports for accounting and diagnostics."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic_ai import ModelMessage, ModelResponse

from derp.inference_types import InferenceReport
from derp.inference_usage import InferenceTokenUsage

_COST_QUANTUM = Decimal("0.000000000001")


def reports_from_messages(
    messages: list[ModelMessage] | tuple[ModelMessage, ...],
    *,
    requested_model: str,
) -> tuple[InferenceReport, ...]:
    """Extract one report per Pydantic AI model response."""
    return tuple(
        report_from_response(message, requested_model=requested_model)
        for message in messages
        if isinstance(message, ModelResponse)
    )


def report_from_response(
    response: ModelResponse,
    *,
    requested_model: str,
) -> InferenceReport:
    """Normalize usage and OpenRouter provider details from one response."""
    details = response.provider_details or {}
    usage = response.usage
    reasoning_tokens = _detail_int(
        usage.details,
        "reasoning_tokens",
        "reasoning",
    )
    generation_id = _detail_text(
        details,
        "generation_id",
        "generationId",
    )
    if (
        generation_id is None
        and response.provider_name == "openrouter"
        and response.provider_response_id
        and response.provider_response_id.startswith("gen-")
    ):
        generation_id = response.provider_response_id
    return InferenceReport(
        provider=response.provider_name or "unknown",
        requested_model=requested_model,
        actual_model=response.model_name or requested_model,
        downstream_provider=_detail_text(details, "downstream_provider"),
        provider_response_id=response.provider_response_id,
        generation_id=generation_id,
        finish_reason=(
            str(response.finish_reason) if response.finish_reason is not None else None
        ),
        tokens=InferenceTokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            audio_input_tokens=usage.input_audio_tokens,
            audio_output_tokens=usage.output_audio_tokens,
        ),
        actual_cost_usd=_cost(details.get("cost")),
    )


def aggregate_reports(
    reports: tuple[InferenceReport, ...],
) -> tuple[InferenceTokenUsage | None, Decimal | None]:
    """Aggregate a multi-request agent run without losing cost availability."""
    if not reports:
        return None, None
    available_tokens = tuple(
        report.tokens for report in reports if report.tokens is not None
    )
    tokens = (
        InferenceTokenUsage(
            input_tokens=sum(usage.input_tokens for usage in available_tokens),
            output_tokens=sum(usage.output_tokens for usage in available_tokens),
            total_tokens=(
                sum(usage.total_tokens for usage in available_tokens)  # type: ignore[misc]
                if all(usage.total_tokens is not None for usage in available_tokens)
                else None
            ),
            cache_read_tokens=sum(
                usage.cache_read_tokens for usage in available_tokens
            ),
            cache_write_tokens=sum(
                usage.cache_write_tokens for usage in available_tokens
            ),
            reasoning_tokens=sum(usage.reasoning_tokens for usage in available_tokens),
            audio_input_tokens=sum(
                usage.audio_input_tokens for usage in available_tokens
            ),
            audio_output_tokens=sum(
                usage.audio_output_tokens for usage in available_tokens
            ),
        )
        if len(available_tokens) == len(reports)
        else None
    )
    costs = [report.actual_cost_usd for report in reports]
    cost = sum(costs, Decimal(0)) if all(value is not None for value in costs) else None
    return tokens, cost


def _detail_int(details: dict[str, int], *names: str) -> int:
    for name in names:
        value = details.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _detail_text(details: dict[str, object], *names: str) -> str | None:
    for name in names:
        value = details.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _cost(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except ValueError, ArithmeticError:
        return None
    if not cost.is_finite() or cost < 0:
        return None
    return cost.quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)


__all__ = [
    "InferenceReport",
    "aggregate_reports",
    "report_from_response",
    "reports_from_messages",
]
