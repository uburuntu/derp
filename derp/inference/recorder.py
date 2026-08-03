"""Inference attempt lifecycle over the content-free usage repository."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic_ai import ModelMessage

from derp.catalog import ModelSpec, TokenPricing
from derp.inference.report import (
    InferenceReport,
    aggregate_reports,
    reports_from_messages,
)
from derp.inference_usage import (
    CostReconciliationStatus,
    InferenceOutcome,
    InferenceUsageCompletion,
    InferenceUsageId,
    InferenceUsageRepository,
    InferenceUsageStart,
)


@dataclass(frozen=True, slots=True)
class InferenceAttempt:
    """Opaque identity registered before a provider request."""

    id: InferenceUsageId
    model: ModelSpec


class InferenceRoutePolicyError(RuntimeError):
    """The provider response identified a route outside the reviewed policy."""


class InferenceRecorder:
    """Record provider attempts and reconcile immediately reported cost."""

    def __init__(self, repository: InferenceUsageRepository) -> None:
        self._repository = repository

    async def start(
        self,
        *,
        model: ModelSpec,
        user_id: uuid.UUID,
        chat_id: uuid.UUID | None,
        operation_id: uuid.UUID | None,
        started_at: datetime | None = None,
    ) -> InferenceAttempt:
        attempt = InferenceAttempt(InferenceUsageId.new(), model)
        await self._repository.register(
            InferenceUsageStart(
                id=attempt.id,
                operation_id=operation_id,
                user_id=user_id,
                chat_id=chat_id,
                provider=model.provider.value,
                provider_model_id=model.provider_model_id,
                started_at=started_at or datetime.now(UTC),
            )
        )
        return attempt

    async def succeed(
        self,
        attempt: InferenceAttempt,
        messages: list[ModelMessage] | tuple[ModelMessage, ...],
        *,
        completed_at: datetime | None = None,
    ) -> tuple[InferenceReport, ...]:
        reports = reports_from_messages(
            messages,
            requested_model=attempt.model.provider_model_id,
        )
        await self.succeed_reports(
            attempt,
            reports,
            completed_at=completed_at,
        )
        return reports

    async def succeed_reports(
        self,
        attempt: InferenceAttempt,
        reports: tuple[InferenceReport, ...],
        *,
        completed_at: datetime | None = None,
    ) -> None:
        """Complete an attempt from already normalized content-free reports."""
        if any(not isinstance(report, InferenceReport) for report in reports):
            raise TypeError("reports must contain only InferenceReport values")
        await self._complete(
            attempt,
            outcome=InferenceOutcome.SUCCEEDED,
            reports=reports,
            completed_at=completed_at,
        )

    async def fail(
        self,
        attempt: InferenceAttempt,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        await self._complete(
            attempt,
            outcome=InferenceOutcome.FAILED,
            reports=(),
            completed_at=completed_at,
        )

    async def _complete(
        self,
        attempt: InferenceAttempt,
        *,
        outcome: InferenceOutcome,
        reports: tuple[InferenceReport, ...],
        completed_at: datetime | None,
    ) -> None:
        timestamp = completed_at or datetime.now(UTC)
        tokens, cost = aggregate_reports(reports)
        last = reports[-1] if reports else None
        if cost is None and reports and _has_zero_token_price(attempt.model):
            cost = Decimal(0)
        generation_id = last.generation_id if len(reports) == 1 else None
        cost_status = (
            CostReconciliationStatus.RECONCILED
            if cost is not None
            else (
                CostReconciliationStatus.PENDING
                if generation_id is not None
                else CostReconciliationStatus.UNAVAILABLE
            )
        )
        route_policy_matched = _route_policy_matches(attempt.model, reports)
        await self._repository.complete(
            attempt.id,
            InferenceUsageCompletion(
                outcome=outcome,
                completed_at=timestamp,
                tokens=tokens,
                provider_response_id=last and last.provider_response_id,
                provider_generation_id=generation_id,
                actual_model_id=last and last.actual_model,
                downstream_provider=last and last.downstream_provider,
                actual_cost_usd=cost,
                route_policy_matched=route_policy_matched,
                cost_reconciliation_status=cost_status,
            ),
        )
        if not route_policy_matched:
            raise InferenceRoutePolicyError(
                "provider response did not match the reviewed inference route"
            )


def _has_zero_token_price(model: ModelSpec) -> bool:
    pricing = model.pricing
    return isinstance(pricing, TokenPricing) and all(
        band.input_per_million == 0 and band.output_per_million == 0
        for band in pricing.bands
    )


def _route_policy_matches(
    model: ModelSpec,
    reports: tuple[InferenceReport, ...],
) -> bool:
    accepted_models = {model.provider_model_id}
    if model.canonical_model_id:
        accepted_models.add(model.canonical_model_id)
    expected_providers = (
        {_normalize_identifier(provider) for provider in model.routing.provider_order}
        if model.routing
        else set()
    )
    for report in reports:
        if report.actual_model not in accepted_models:
            return False
        if report.downstream_provider and expected_providers:
            actual = _normalize_identifier(report.downstream_provider)
            if not any(
                actual == expected or actual.startswith(f"{expected}-")
                for expected in expected_providers
            ):
                return False
    return True


def _normalize_identifier(value: str) -> str:
    return "-".join(value.casefold().replace("/", "-").split())


__all__ = ["InferenceAttempt", "InferenceRecorder", "InferenceRoutePolicyError"]
