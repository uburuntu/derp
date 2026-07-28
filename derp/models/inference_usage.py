"""Content-free provider inference usage and cost reconciliation state."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class InferenceUsage(TimestampMixin, Base):
    """One provider attempt with content-free usage and actual USD cost."""

    __tablename__ = "inference_usage_records"
    __table_args__ = (
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'pending'::text, 'succeeded'::text, 'failed'::text])",
            name="inference_usage_status_allowed",
        ),
        CheckConstraint(
            "reconciliation_status::text = ANY (ARRAY["
            "'not_ready'::text, 'pending'::text, 'reconciled'::text, "
            "'unavailable'::text])",
            name="inference_usage_reconciliation_status_allowed",
        ),
        CheckConstraint(
            "length(btrim(provider::text)) > 0 "
            "AND length(btrim(provider_model_id::text)) > 0",
            name="inference_usage_provider_fields_not_blank",
        ),
        CheckConstraint(
            "provider_response_id IS NULL "
            "OR length(btrim(provider_response_id::text)) > 0",
            name="inference_usage_response_id_not_blank",
        ),
        CheckConstraint(
            "provider_generation_id IS NULL "
            "OR length(btrim(provider_generation_id::text)) > 0",
            name="inference_usage_generation_id_not_blank",
        ),
        CheckConstraint(
            "actual_model_id IS NULL OR length(btrim(actual_model_id::text)) > 0",
            name="inference_usage_actual_model_not_blank",
        ),
        CheckConstraint(
            "downstream_provider IS NULL "
            "OR length(btrim(downstream_provider::text)) > 0",
            name="inference_usage_downstream_provider_not_blank",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 "
            "AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 "
            "AND reasoning_tokens >= 0 AND audio_input_tokens >= 0 "
            "AND audio_output_tokens >= 0 "
            "AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="inference_usage_tokens_non_negative",
        ),
        CheckConstraint(
            "usage_available OR input_tokens = 0 AND output_tokens = 0 "
            "AND total_tokens IS NULL AND cache_read_tokens = 0 "
            "AND cache_write_tokens = 0 AND reasoning_tokens = 0 "
            "AND audio_input_tokens = 0 AND audio_output_tokens = 0",
            name="inference_usage_unavailable_tokens_empty",
        ),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0::numeric",
            name="inference_usage_actual_cost_non_negative",
        ),
        CheckConstraint(
            "provider_completed_at IS NULL "
            "OR provider_completed_at >= provider_started_at",
            name="inference_usage_provider_time_order",
        ),
        CheckConstraint(
            "usage_recorded_at IS NULL OR usage_recorded_at >= provider_started_at",
            name="inference_usage_recorded_time_order",
        ),
        CheckConstraint(
            "reconciliation_requested_at IS NULL "
            "OR usage_recorded_at IS NOT NULL "
            "AND reconciliation_requested_at >= usage_recorded_at",
            name="inference_usage_reconciliation_requires_usage",
        ),
        CheckConstraint(
            "reconciliation_attempts >= 0",
            name="inference_usage_reconciliation_attempts_non_negative",
        ),
        CheckConstraint(
            "num_nonnulls(reconciliation_claim_token, reconciliation_claimed_at, "
            "reconciliation_lease_expires_at) = ANY (ARRAY[0, 3])",
            name="inference_usage_reconciliation_claim_complete",
        ),
        CheckConstraint(
            "reconciliation_claimed_at IS NULL OR "
            "reconciliation_requested_at IS NOT NULL AND "
            "reconciliation_claimed_at >= reconciliation_requested_at AND "
            "reconciliation_lease_expires_at > reconciliation_claimed_at",
            name="inference_usage_reconciliation_claim_time_order",
        ),
        CheckConstraint(
            "reconciliation_retry_at IS NULL OR "
            "reconciliation_status::text = 'pending'::text AND "
            "reconciliation_requested_at IS NOT NULL AND "
            "reconciliation_retry_at >= reconciliation_requested_at",
            name="inference_usage_reconciliation_retry_pending",
        ),
        CheckConstraint(
            "reconciliation_claim_token IS NULL OR reconciliation_retry_at IS NULL",
            name="inference_usage_reconciliation_claim_not_deferred",
        ),
        CheckConstraint(
            "reconciled_at IS NULL OR reconciliation_requested_at IS NOT NULL "
            "AND reconciled_at >= reconciliation_requested_at",
            name="inference_usage_reconciled_requires_request",
        ),
        CheckConstraint(
            "status::text = 'pending'::text "
            "AND provider_completed_at IS NULL AND usage_recorded_at IS NULL "
            "AND provider_response_id IS NULL AND provider_generation_id IS NULL "
            "AND NOT usage_available "
            "AND reconciliation_status::text = 'not_ready'::text "
            "OR (status::text = ANY (ARRAY['succeeded'::text, 'failed'::text])) "
            "AND provider_completed_at IS NOT NULL AND usage_recorded_at IS NOT NULL "
            "AND (reconciliation_status::text = ANY "
            "(ARRAY['pending'::text, 'reconciled'::text, 'unavailable'::text]))",
            name="inference_usage_outcome_state_matches",
        ),
        CheckConstraint(
            "reconciliation_status::text = 'not_ready'::text "
            "AND reconciliation_requested_at IS NULL AND reconciled_at IS NULL "
            "AND actual_cost_usd IS NULL AND reconciliation_retry_at IS NULL "
            "AND reconciliation_claim_token IS NULL "
            "AND reconciliation_attempts = 0 "
            "OR reconciliation_status::text = 'pending'::text "
            "AND reconciliation_requested_at IS NOT NULL AND reconciled_at IS NULL "
            "AND actual_cost_usd IS NULL "
            "OR reconciliation_status::text = 'reconciled'::text "
            "AND reconciliation_requested_at IS NOT NULL "
            "AND reconciled_at IS NOT NULL AND actual_cost_usd IS NOT NULL "
            "AND reconciliation_retry_at IS NULL "
            "AND reconciliation_claim_token IS NULL "
            "OR reconciliation_status::text = 'unavailable'::text "
            "AND reconciled_at IS NULL "
            "AND actual_cost_usd IS NULL AND reconciliation_retry_at IS NULL "
            "AND reconciliation_claim_token IS NULL",
            name="inference_usage_reconciliation_state_matches",
        ),
        Index("idx_inference_usage_operation", "operation_id", "provider_started_at"),
        Index("idx_inference_usage_user", "user_id", "provider_started_at"),
        Index("idx_inference_usage_chat", "chat_id", "provider_started_at"),
        Index(
            "idx_inference_usage_provider_response",
            "provider",
            "provider_response_id",
        ),
        Index(
            "idx_inference_usage_provider_generation",
            "provider",
            "provider_generation_id",
        ),
        Index(
            "idx_inference_usage_reconciliation",
            "reconciliation_status",
            "reconciliation_requested_at",
            "id",
        ),
        Index(
            "idx_inference_usage_reconciliation_claim",
            "provider",
            "reconciliation_status",
            "reconciliation_retry_at",
            "reconciliation_lease_expires_at",
            "reconciliation_requested_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paid_operations.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chats.id", ondelete="RESTRICT"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    provider_model_id: Mapped[str] = mapped_column(String(255))
    provider_response_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_generation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    actual_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    downstream_provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    route_policy_matched: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    audio_input_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    audio_output_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    usage_available: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 12), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    reconciliation_status: Mapped[str] = mapped_column(
        String(16), default="not_ready", server_default=text("'not_ready'")
    )
    provider_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    usage_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_claim_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    reconciliation_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["InferenceUsage"]
