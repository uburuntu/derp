"""Bounded, privacy-safe OpenRouter cost reconciliation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from derp.inference.reconciliation import (
    OpenRouterCostReconciliationReport,
    OpenRouterCostReconciliationService,
)
from derp.inference_usage import (
    CostReconciliationStatus,
    InferenceCostReconciliationClaim,
    InferenceReconciliationClaimLostError,
    InferenceTokenUsage,
    InferenceUsageId,
    InferenceUsageRepository,
)
from derp.openrouter import (
    GenerationMetadata,
    OpenRouterHTTPError,
    OpenRouterTransportError,
    TransportFailureKind,
)

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _claim(
    *,
    generation_id: str | None = "gen-1",
    attempt: int = 1,
) -> InferenceCostReconciliationClaim:
    return InferenceCostReconciliationClaim(
        usage_id=InferenceUsageId.new(),
        token=uuid4(),
        provider_generation_id=generation_id,
        expected_tokens=InferenceTokenUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=15),
        attempt=attempt,
    )


def _metadata(
    generation_id: str = "gen-1",
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cost: Decimal = Decimal("0.001234567890"),
) -> GenerationMetadata:
    return GenerationMetadata(
        id=generation_id,
        model="provider/model",
        created_at=NOW,
        provider_name="Provider",
        upstream_id=None,
        total_cost=cost,
        usage=cost,
        upstream_inference_cost=None,
        is_byok=False,
        cancelled=False,
        finish_reason="stop",
        native_finish_reason="STOP",
        tokens_prompt=prompt_tokens,
        tokens_completion=completion_tokens,
        native_tokens_prompt=prompt_tokens,
        native_tokens_completion=completion_tokens,
        native_tokens_cached=0,
        native_tokens_reasoning=0,
        num_input_audio_prompt=0,
        generation_time=Decimal("100"),
        latency=Decimal("120"),
    )


def _repository(*claims: InferenceCostReconciliationClaim) -> MagicMock:
    repository = MagicMock(spec=InferenceUsageRepository)
    repository.fail_stale_attempts = AsyncMock(return_value=0)
    repository.claim_pending_reconciliation = AsyncMock(return_value=claims)
    repository.reconcile_claimed_cost = AsyncMock()
    pending = MagicMock()
    pending.reconciliation_status = CostReconciliationStatus.PENDING
    repository.defer_cost_reconciliation = AsyncMock(return_value=pending)
    unavailable = MagicMock()
    unavailable.reconciliation_status = CostReconciliationStatus.UNAVAILABLE
    repository.mark_claimed_cost_unavailable = AsyncMock(return_value=unavailable)
    return repository


def _client(result: GenerationMetadata | Exception) -> MagicMock:
    client = MagicMock()
    client.get_generation = AsyncMock(
        side_effect=result if isinstance(result, Exception) else None,
        return_value=None if isinstance(result, Exception) else result,
    )
    return client


@pytest.mark.asyncio
async def test_sweep_reports_bounded_stale_attempt_cleanup() -> None:
    repository = _repository()
    repository.fail_stale_attempts.return_value = 3
    service = OpenRouterCostReconciliationService(
        repository,
        _client(_metadata()),
        clock=lambda: NOW,
    )

    report = await service.reconcile(limit=7)

    assert report == OpenRouterCostReconciliationReport(0, 0, 0, 0, 0, 3)
    repository.claim_pending_reconciliation.assert_awaited_once()
    repository.fail_stale_attempts.assert_awaited_once_with(
        provider="openrouter",
        started_before=NOW - timedelta(hours=1),
        failed_at=NOW,
        limit=7,
    )


@pytest.mark.asyncio
async def test_exact_decimal_cost_is_recorded_under_the_owned_claim() -> None:
    claim = _claim()
    repository = _repository(claim)
    client = _client(_metadata())
    service = OpenRouterCostReconciliationService(
        repository,
        client,
        clock=lambda: NOW,
    )

    with patch("derp.inference.reconciliation.logfire.info") as info:
        report = await service.reconcile(limit=10)

    assert report == OpenRouterCostReconciliationReport(1, 1, 0, 0, 0)
    assert not {
        "usage_id",
        "generation_id",
        "user_id",
        "chat_id",
    } & set(report.__dataclass_fields__)
    repository.claim_pending_reconciliation.assert_awaited_once_with(
        provider="openrouter",
        now=NOW,
        lease_duration=timedelta(minutes=15),
        limit=10,
    )
    repository.fail_stale_attempts.assert_awaited_once_with(
        provider="openrouter",
        started_before=NOW - timedelta(hours=1),
        failed_at=NOW,
        limit=10,
    )
    reconciliation = repository.reconcile_claimed_cost.await_args.args[1]
    assert reconciliation.actual_cost_usd == Decimal("0.001234567890")
    assert reconciliation.reconciled_at == NOW
    repository.defer_cost_reconciliation.assert_not_awaited()
    logged = info.call_args.kwargs
    assert set(logged) == {
        "claimed_count",
        "reconciled_count",
        "retry_scheduled_count",
        "unavailable_count",
        "claim_lost_count",
        "stale_attempt_count",
    }
    assert "gen-1" not in repr(info.call_args)


@pytest.mark.parametrize(
    ("error", "expected_report", "delay"),
    [
        (
            OpenRouterHTTPError(
                status_code=429,
                method="GET",
                endpoint="/generation",
                retry_after="120",
            ),
            OpenRouterCostReconciliationReport(1, 0, 1, 0, 0),
            timedelta(seconds=120),
        ),
        (
            OpenRouterTransportError(
                kind=TransportFailureKind.TIMEOUT,
                method="GET",
                endpoint="/generation",
            ),
            OpenRouterCostReconciliationReport(1, 0, 1, 0, 0),
            timedelta(seconds=30),
        ),
        (
            OpenRouterHTTPError(
                status_code=404,
                method="GET",
                endpoint="/generation",
            ),
            OpenRouterCostReconciliationReport(1, 0, 0, 1, 0),
            timedelta(minutes=5),
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_uncertainty_is_retried_or_closed_by_error_class(
    error: Exception,
    expected_report: OpenRouterCostReconciliationReport,
    delay: timedelta,
) -> None:
    claim = _claim()
    repository = _repository(claim)
    service = OpenRouterCostReconciliationService(
        repository,
        _client(error),
        clock=lambda: NOW,
    )

    report = await service.reconcile()

    assert report == expected_report
    repository.reconcile_claimed_cost.assert_not_awaited()
    if expected_report.retry_scheduled_count:
        assert repository.defer_cost_reconciliation.await_args.kwargs == {
            "deferred_at": NOW,
            "retry_at": NOW + delay,
        }
        repository.mark_claimed_cost_unavailable.assert_not_awaited()
    else:
        repository.defer_cost_reconciliation.assert_not_awaited()
        repository.mark_claimed_cost_unavailable.assert_awaited_once_with(
            claim,
            unavailable_at=NOW,
        )


@pytest.mark.asyncio
async def test_missing_generation_id_becomes_terminal_without_http_call() -> None:
    claim = _claim(generation_id=None)
    repository = _repository(claim)
    client = _client(_metadata())
    service = OpenRouterCostReconciliationService(
        repository,
        client,
        clock=lambda: NOW,
    )

    report = await service.reconcile()

    assert report == OpenRouterCostReconciliationReport(1, 0, 0, 1, 0)
    client.get_generation.assert_not_awaited()
    repository.defer_cost_reconciliation.assert_not_awaited()
    repository.mark_claimed_cost_unavailable.assert_awaited_once_with(
        claim,
        unavailable_at=NOW,
    )


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata("different-generation"),
        _metadata(prompt_tokens=99),
        _metadata(completion_tokens=49),
    ],
)
@pytest.mark.asyncio
async def test_mismatched_metadata_never_records_approximate_cost(
    metadata: GenerationMetadata,
) -> None:
    claim = _claim()
    repository = _repository(claim)
    service = OpenRouterCostReconciliationService(
        repository,
        _client(metadata),
        clock=lambda: NOW,
    )

    report = await service.reconcile()

    assert report == OpenRouterCostReconciliationReport(1, 0, 0, 1, 0)
    repository.reconcile_claimed_cost.assert_not_awaited()
    repository.defer_cost_reconciliation.assert_not_awaited()
    repository.mark_claimed_cost_unavailable.assert_awaited_once_with(
        claim,
        unavailable_at=NOW,
    )


@pytest.mark.asyncio
async def test_overprecision_becomes_terminal_instead_of_being_rounded() -> None:
    claim = _claim()
    repository = _repository(claim)
    service = OpenRouterCostReconciliationService(
        repository,
        _client(_metadata(cost=Decimal("0.0000000000001"))),
        clock=lambda: NOW,
    )

    report = await service.reconcile()

    assert report == OpenRouterCostReconciliationReport(1, 0, 0, 1, 0)
    repository.reconcile_claimed_cost.assert_not_awaited()
    repository.defer_cost_reconciliation.assert_not_awaited()
    repository.mark_claimed_cost_unavailable.assert_awaited_once_with(
        claim,
        unavailable_at=NOW,
    )


@pytest.mark.parametrize(
    "error",
    [
        OpenRouterHTTPError(
            status_code=429,
            method="GET",
            endpoint="/generation",
        ),
        OpenRouterTransportError(
            kind=TransportFailureKind.TIMEOUT,
            method="GET",
            endpoint="/generation",
        ),
    ],
)
@pytest.mark.asyncio
async def test_retryable_failures_become_terminal_after_bounded_attempts(
    error: Exception,
) -> None:
    claim = _claim(attempt=8)
    repository = _repository(claim)
    service = OpenRouterCostReconciliationService(
        repository,
        _client(error),
        clock=lambda: NOW,
    )

    report = await service.reconcile()

    assert report == OpenRouterCostReconciliationReport(1, 0, 0, 1, 0)
    repository.defer_cost_reconciliation.assert_not_awaited()
    repository.mark_claimed_cost_unavailable.assert_awaited_once()


@pytest.mark.asyncio
async def test_lost_lease_is_observed_without_overwriting_new_owner() -> None:
    claim = _claim()
    repository = _repository(claim)
    repository.reconcile_claimed_cost.side_effect = (
        InferenceReconciliationClaimLostError("lease lost")
    )
    service = OpenRouterCostReconciliationService(
        repository,
        _client(_metadata()),
        clock=lambda: NOW,
    )

    report = await service.reconcile()

    assert report == OpenRouterCostReconciliationReport(1, 0, 0, 0, 1)
    repository.defer_cost_reconciliation.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_fetch_concurrency_is_bounded() -> None:
    claims = tuple(_claim(generation_id=f"gen-{index}") for index in range(8))
    repository = _repository(*claims)
    active = 0
    maximum = 0

    async def get_generation(generation_id: str) -> GenerationMetadata:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return _metadata(generation_id)

    client = MagicMock()
    client.get_generation = AsyncMock(side_effect=get_generation)
    service = OpenRouterCostReconciliationService(
        repository,
        client,
        clock=lambda: NOW,
        concurrency=2,
    )

    report = await service.reconcile(limit=8)

    assert report == OpenRouterCostReconciliationReport(8, 8, 0, 0, 0)
    assert maximum == 2


@pytest.mark.asyncio
async def test_unexpected_failure_propagates_and_leaves_lease_to_expire() -> None:
    claim = _claim()
    repository = _repository(claim)
    service = OpenRouterCostReconciliationService(
        repository,
        _client(RuntimeError("private remote detail")),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="private remote detail"):
        await service.reconcile()

    repository.reconcile_claimed_cost.assert_not_awaited()
    repository.defer_cost_reconciliation.assert_not_awaited()
