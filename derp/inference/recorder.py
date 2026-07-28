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
    InferenceCostReconciliation,
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
            CostReconciliationStatus.PENDING
            if cost is not None or generation_id is not None
            else CostReconciliationStatus.UNAVAILABLE
        )
        await self._repository.complete(
            attempt.id,
            InferenceUsageCompletion(
                outcome=outcome,
                completed_at=timestamp,
                tokens=tokens,
                provider_response_id=last and last.provider_response_id,
                provider_generation_id=generation_id,
                cost_reconciliation_status=cost_status,
            ),
        )
        if cost is not None:
            await self._repository.reconcile_cost(
                attempt.id,
                InferenceCostReconciliation(
                    actual_cost_usd=cost,
                    reconciled_at=timestamp,
                ),
            )


def _has_zero_token_price(model: ModelSpec) -> bool:
    pricing = model.pricing
    return isinstance(pricing, TokenPricing) and all(
        band.input_per_million == 0 and band.output_per_million == 0
        for band in pricing.bands
    )


__all__ = ["InferenceAttempt", "InferenceRecorder"]
