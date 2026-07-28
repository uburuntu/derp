"""Content-free Pydantic AI response normalization and recording."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic_ai import ModelResponse, RequestUsage, TextPart

from derp.catalog import ModelRole, get_openrouter_model
from derp.inference import (
    InferenceRecorder,
    aggregate_reports,
    report_from_response,
    reports_from_messages,
)
from derp.inference_usage import (
    InferenceOutcome,
    InferenceUsageRepository,
)

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _response(
    *,
    cost: float | None = 0.0012345678914,
    generation_detail: bool = True,
    response_id: str = "response-1",
) -> ModelResponse:
    details: dict[str, object] = {
        "downstream_provider": "anthropic",
    }
    if generation_detail:
        details["generation_id"] = "gen-1"
    if cost is not None:
        details["cost"] = cost
    return ModelResponse(
        parts=[TextPart("private response content")],
        usage=RequestUsage(
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=80,
            cache_write_tokens=10,
            input_audio_tokens=7,
            output_audio_tokens=3,
            details={"reasoning_tokens": 9},
        ),
        model_name="anthropic/claude-sonnet-5",
        timestamp=NOW,
        provider_name="openrouter",
        provider_details=details,
        provider_response_id=response_id,
        finish_reason="stop",
    )


def test_response_report_captures_usage_and_no_content() -> None:
    report = report_from_response(
        _response(),
        requested_model="anthropic/claude-sonnet-5",
    )

    assert report.provider == "openrouter"
    assert report.downstream_provider == "anthropic"
    assert report.provider_response_id == "response-1"
    assert report.generation_id == "gen-1"
    assert report.tokens.input_tokens == 100
    assert report.tokens.cache_read_tokens == 80
    assert report.tokens.cache_write_tokens == 10
    assert report.tokens.reasoning_tokens == 9
    assert report.tokens.audio_input_tokens == 7
    assert report.tokens.audio_output_tokens == 3
    assert report.actual_cost_usd == Decimal("0.001234567891")
    assert not {"prompt", "content", "response_text"} & set(report.__dataclass_fields__)


def test_openrouter_generation_response_id_is_used_when_mapper_omits_detail() -> None:
    report = report_from_response(
        _response(generation_detail=False, response_id="gen-live-1"),
        requested_model="anthropic/claude-sonnet-5",
    )

    assert report.generation_id == "gen-live-1"


def test_reports_preserve_each_model_response_and_aggregate_known_costs() -> None:
    reports = reports_from_messages(
        [_response(cost=0.01), _response(cost=0.02)],
        requested_model="anthropic/claude-sonnet-5",
    )
    tokens, cost = aggregate_reports(reports)

    assert len(reports) == 2
    assert tokens is not None
    assert tokens.input_tokens == 200
    assert tokens.total_tokens == 240
    assert cost == Decimal("0.030000000000")


def test_aggregate_cost_remains_pending_when_any_response_omits_cost() -> None:
    reports = reports_from_messages(
        [_response(cost=0.01), _response(cost=None)],
        requested_model="anthropic/claude-sonnet-5",
    )

    _, cost = aggregate_reports(reports)

    assert cost is None


def test_aggregate_tokens_remain_unavailable_when_any_report_omits_usage() -> None:
    report = report_from_response(
        _response(),
        requested_model="anthropic/claude-sonnet-5",
    )
    without_usage = report.__class__(
        provider=report.provider,
        requested_model=report.requested_model,
        actual_model=report.actual_model,
        downstream_provider=report.downstream_provider,
        provider_response_id=report.provider_response_id,
        generation_id=report.generation_id,
        finish_reason=report.finish_reason,
        tokens=None,
        actual_cost_usd=None,
    )

    tokens, cost = aggregate_reports((report, without_usage))

    assert tokens is None
    assert cost is None


@pytest.mark.asyncio
async def test_recorder_preserves_generation_id_without_token_usage() -> None:
    repository = MagicMock(spec=InferenceUsageRepository)
    repository.register = AsyncMock()
    repository.complete = AsyncMock()
    repository.reconcile_cost = AsyncMock()
    recorder = InferenceRecorder(repository)
    attempt = await recorder.start(
        model=get_openrouter_model(ModelRole.CHAT_STANDARD),
        user_id=UUID(int=1),
        chat_id=None,
        operation_id=None,
        started_at=NOW,
    )
    report = report_from_response(
        _response(cost=None),
        requested_model=attempt.model.provider_model_id,
    )
    report = report.__class__(
        provider=report.provider,
        requested_model=report.requested_model,
        actual_model=report.actual_model,
        downstream_provider=report.downstream_provider,
        provider_response_id=report.provider_response_id,
        generation_id=report.generation_id,
        finish_reason=report.finish_reason,
        tokens=None,
        actual_cost_usd=None,
    )

    await recorder.succeed_reports(attempt, (report,), completed_at=NOW)

    completion = repository.complete.await_args.args[1]
    assert completion.tokens is None
    assert completion.provider_generation_id == "gen-1"
    assert completion.cost_reconciliation_status.value == "pending"


@pytest.mark.asyncio
async def test_recorder_registers_before_completion_and_reconciles_reported_cost() -> (
    None
):
    repository = MagicMock(spec=InferenceUsageRepository)
    repository.register = AsyncMock()
    repository.complete = AsyncMock()
    repository.reconcile_cost = AsyncMock()
    recorder = InferenceRecorder(repository)
    model = get_openrouter_model(ModelRole.CHAT_STANDARD)

    attempt = await recorder.start(
        model=model,
        user_id=UUID(int=1),
        chat_id=UUID(int=2),
        operation_id=UUID(int=3),
        started_at=NOW,
    )
    reports = await recorder.succeed(attempt, [_response()], completed_at=NOW)

    start = repository.register.await_args.args[0]
    completion = repository.complete.await_args.args[1]
    reconciliation = repository.reconcile_cost.await_args.args[1]
    assert start.id == attempt.id
    assert start.user_id == UUID(int=1)
    assert start.provider == "openrouter"
    assert completion.outcome is InferenceOutcome.SUCCEEDED
    assert completion.provider_response_id == "response-1"
    assert completion.provider_generation_id == "gen-1"
    assert reconciliation.actual_cost_usd == reports[0].actual_cost_usd


@pytest.mark.asyncio
async def test_failed_attempt_is_terminal_without_fabricated_usage() -> None:
    repository = MagicMock(spec=InferenceUsageRepository)
    repository.register = AsyncMock()
    repository.complete = AsyncMock()
    repository.reconcile_cost = AsyncMock()
    recorder = InferenceRecorder(repository)
    attempt = await recorder.start(
        model=get_openrouter_model(ModelRole.CHAT_STANDARD),
        user_id=UUID(int=1),
        chat_id=None,
        operation_id=None,
        started_at=NOW,
    )

    await recorder.fail(attempt, completed_at=NOW)

    completion = repository.complete.await_args.args[1]
    assert completion.outcome is InferenceOutcome.FAILED
    assert completion.tokens is None
    repository.reconcile_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_response_run_does_not_bind_aggregate_usage_to_one_generation() -> (
    None
):
    repository = MagicMock(spec=InferenceUsageRepository)
    repository.register = AsyncMock()
    repository.complete = AsyncMock()
    repository.reconcile_cost = AsyncMock()
    recorder = InferenceRecorder(repository)
    attempt = await recorder.start(
        model=get_openrouter_model(ModelRole.CHAT_STANDARD),
        user_id=UUID(int=1),
        chat_id=None,
        operation_id=None,
        started_at=NOW,
    )

    await recorder.succeed(
        attempt,
        [_response(cost=None), _response(cost=None)],
        completed_at=NOW,
    )

    completion = repository.complete.await_args.args[1]
    assert completion.provider_generation_id is None
    repository.reconcile_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_catalog_run_reconciles_missing_upstream_zero_cost() -> None:
    repository = MagicMock(spec=InferenceUsageRepository)
    repository.register = AsyncMock()
    repository.complete = AsyncMock()
    repository.reconcile_cost = AsyncMock()
    recorder = InferenceRecorder(repository)
    attempt = await recorder.start(
        model=get_openrouter_model(ModelRole.FREE_TEXT),
        user_id=UUID(int=1),
        chat_id=None,
        operation_id=None,
        started_at=NOW,
    )

    await recorder.succeed(attempt, [_response(cost=None)], completed_at=NOW)

    reconciliation = repository.reconcile_cost.await_args.args[1]
    assert reconciliation.actual_cost_usd == Decimal(0)
