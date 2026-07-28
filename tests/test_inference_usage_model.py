"""Schema contract for content-free inference usage persistence."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric

from derp.models.inference_usage import InferenceUsage


def test_inference_usage_schema_carries_only_content_free_operational_facts() -> None:
    columns = InferenceUsage.__table__.columns
    names = set(columns.keys())

    assert {
        "operation_id",
        "user_id",
        "chat_id",
        "provider",
        "provider_model_id",
        "provider_response_id",
        "provider_generation_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "actual_cost_usd",
        "status",
        "reconciliation_status",
        "provider_started_at",
        "provider_completed_at",
        "usage_recorded_at",
        "reconciliation_requested_at",
        "reconciliation_retry_at",
        "reconciliation_claim_token",
        "reconciliation_claimed_at",
        "reconciliation_lease_expires_at",
        "reconciliation_attempts",
        "reconciled_at",
    } <= names
    assert (
        not {
            "telegram_id",
            "message_id",
            "prompt",
            "query",
            "content",
            "request_body",
            "response_body",
            "tool_arguments",
        }
        & names
    )
    assert all("content" not in name and "telegram" not in name for name in names)


def test_inference_usage_uses_database_uuid_foreign_keys() -> None:
    columns = InferenceUsage.__table__.columns

    assert {key.target_fullname for key in columns.operation_id.foreign_keys} == {
        "paid_operations.id"
    }
    assert {key.target_fullname for key in columns.user_id.foreign_keys} == {"users.id"}
    assert {key.target_fullname for key in columns.chat_id.foreign_keys} == {"chats.id"}
    assert columns.operation_id.nullable
    assert not columns.user_id.nullable
    assert columns.chat_id.nullable


def test_actual_cost_is_decimal_with_explicit_precision() -> None:
    cost_type = InferenceUsage.__table__.columns.actual_cost_usd.type

    assert isinstance(cost_type, Numeric)
    assert cost_type.precision == 20
    assert cost_type.scale == 12
    assert cost_type.python_type is Decimal


def test_usage_schema_has_state_guards_and_reconciliation_indexes() -> None:
    constraints = {
        constraint.name for constraint in InferenceUsage.__table__.constraints
    }
    indexes = {index.name for index in InferenceUsage.__table__.indexes}

    assert {
        "inference_usage_status_allowed",
        "inference_usage_reconciliation_status_allowed",
        "inference_usage_tokens_non_negative",
        "inference_usage_outcome_state_matches",
        "inference_usage_reconciliation_state_matches",
        "inference_usage_reconciliation_claim_complete",
        "inference_usage_reconciliation_claim_time_order",
        "inference_usage_reconciliation_retry_pending",
    } <= constraints
    assert {
        "idx_inference_usage_operation",
        "idx_inference_usage_user",
        "idx_inference_usage_chat",
        "idx_inference_usage_provider_response",
        "idx_inference_usage_provider_generation",
        "idx_inference_usage_reconciliation",
        "idx_inference_usage_reconciliation_claim",
    } <= indexes
