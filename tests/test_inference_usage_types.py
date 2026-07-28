"""Invariant tests for provider-neutral inference usage contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from derp.inference_usage import (
    InferenceCostReconciliation,
    InferenceCostReconciliationClaim,
    InferenceOutcome,
    InferenceTokenUsage,
    InferenceUsageCompletion,
    InferenceUsageId,
    InferenceUsageStart,
)

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def test_full_provider_neutral_usage_contract_is_content_free() -> None:
    start = InferenceUsageStart(
        id=InferenceUsageId.new(),
        operation_id=uuid4(),
        user_id=uuid4(),
        chat_id=uuid4(),
        provider="provider-a",
        provider_model_id="model-v2",
        started_at=NOW,
    )
    tokens = InferenceTokenUsage(
        input_tokens=101,
        output_tokens=53,
        total_tokens=154,
        cache_read_tokens=80,
        cache_write_tokens=21,
        reasoning_tokens=13,
        audio_input_tokens=8,
        audio_output_tokens=5,
    )
    completion = InferenceUsageCompletion(
        outcome=InferenceOutcome.SUCCEEDED,
        completed_at=NOW,
        tokens=tokens,
        provider_response_id="response-opaque",
        provider_generation_id="generation-opaque",
    )
    reconciliation = InferenceCostReconciliation(
        actual_cost_usd=Decimal("0.001234567890"),
        reconciled_at=NOW,
    )

    assert start.id.value
    assert completion.tokens == tokens
    assert reconciliation.actual_cost_usd == Decimal("0.001234567890")
    assert not {
        "prompt",
        "content",
        "query",
        "response_text",
        "telegram_id",
    } & set(start.__dataclass_fields__)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", -1),
        ("output_tokens", True),
        ("cache_read_tokens", 1.5),
        ("reasoning_tokens", -3),
        ("audio_output_tokens", False),
    ],
)
def test_token_usage_rejects_non_integer_or_negative_counts(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        InferenceTokenUsage(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal("-0.01"), Decimal("NaN"), Decimal("Inf")])
def test_actual_cost_must_be_finite_and_non_negative(value: Decimal) -> None:
    with pytest.raises(ValueError, match="finite, non-negative"):
        InferenceCostReconciliation(actual_cost_usd=value, reconciled_at=NOW)


def test_actual_cost_requires_decimal_and_bounded_scale() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        InferenceCostReconciliation(  # type: ignore[arg-type]
            actual_cost_usd=0.01,
            reconciled_at=NOW,
        )
    with pytest.raises(ValueError, match="at most 12 decimal places"):
        InferenceCostReconciliation(
            actual_cost_usd=Decimal("0.0000000000001"),
            reconciled_at=NOW,
        )


@pytest.mark.parametrize("field", ["started_at", "completed_at", "reconciled_at"])
def test_persistence_timestamps_must_be_timezone_aware(field: str) -> None:
    naive = NOW.replace(tzinfo=None)
    if field == "started_at":
        with pytest.raises(ValueError, match=field):
            InferenceUsageStart(
                id=InferenceUsageId.new(),
                operation_id=None,
                user_id=uuid4(),
                chat_id=None,
                provider="provider",
                provider_model_id="model",
                started_at=naive,
            )
    elif field == "completed_at":
        with pytest.raises(ValueError, match=field):
            InferenceUsageCompletion(
                outcome=InferenceOutcome.FAILED,
                completed_at=naive,
            )
    else:
        with pytest.raises(ValueError, match=field):
            InferenceCostReconciliation(
                actual_cost_usd=Decimal(0),
                reconciled_at=naive,
            )


def test_attempt_identity_requires_database_uuids_and_bounded_provider_ids() -> None:
    with pytest.raises(TypeError, match="user_id"):
        InferenceUsageStart(
            id=InferenceUsageId.new(),
            operation_id=None,
            user_id=42,  # type: ignore[arg-type]
            chat_id=None,
            provider="provider",
            provider_model_id="model",
            started_at=NOW,
        )
    with pytest.raises(ValueError, match="provider must not be blank"):
        InferenceUsageStart(
            id=InferenceUsageId.new(),
            operation_id=None,
            user_id=uuid4(),
            chat_id=None,
            provider=" ",
            provider_model_id="model",
            started_at=NOW,
        )
    with pytest.raises(ValueError, match="provider_model_id exceeds"):
        InferenceUsageStart(
            id=InferenceUsageId.new(),
            operation_id=None,
            user_id=uuid4(),
            chat_id=None,
            provider="provider",
            provider_model_id="m" * 256,
            started_at=NOW,
        )


def test_usage_id_accepts_only_uuid() -> None:
    with pytest.raises(TypeError, match="UUID"):
        InferenceUsageId("not-a-uuid")  # type: ignore[arg-type]


def test_reconciliation_claim_is_content_free_and_expiring() -> None:
    claim = InferenceCostReconciliationClaim(
        usage_id=InferenceUsageId.new(),
        token=uuid4(),
        provider_generation_id="generation-opaque",
        expected_tokens=InferenceTokenUsage(input_tokens=10, output_tokens=5),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
        attempt=1,
    )

    assert claim.attempt == 1
    assert not {
        "user_id",
        "chat_id",
        "telegram_id",
        "prompt",
        "content",
    } & set(claim.__dataclass_fields__)


def test_reconciliation_claim_rejects_invalid_lease_or_attempt() -> None:
    values = {
        "usage_id": InferenceUsageId.new(),
        "token": uuid4(),
        "provider_generation_id": None,
        "expected_tokens": None,
        "claimed_at": NOW,
        "lease_expires_at": NOW + timedelta(minutes=5),
        "attempt": 1,
    }
    with pytest.raises(ValueError, match="lease_expires_at"):
        InferenceCostReconciliationClaim(
            **(values | {"lease_expires_at": NOW})  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="attempt"):
        InferenceCostReconciliationClaim(**(values | {"attempt": 0}))  # type: ignore[arg-type]
