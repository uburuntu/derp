"""Durable artifacts and Telegram delivery intent state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class Artifact(Base):
    """Private TTL-bound metadata for one operation output file."""

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "kind::text = ANY (ARRAY["
            "'image'::text, 'video'::text, 'audio'::text, 'document'::text])",
            name="artifact_kind_allowed",
        ),
        CheckConstraint(
            f"size_bytes > 0 AND size_bytes <= {MAX_ARTIFACT_BYTES}",
            name="artifact_size_bounded",
        ),
        CheckConstraint("ordinal >= 0", name="artifact_ordinal_non_negative"),
        CheckConstraint(
            "length(btrim(mime_type::text)) > 0 AND length(btrim(filename::text)) > 0",
            name="artifact_names_not_blank",
        ),
        CheckConstraint(
            "storage_key::text ~ '^[0-9a-f]{32}$'::text",
            name="artifact_storage_key_opaque",
        ),
        CheckConstraint(
            "sha256::text ~ '^[0-9a-f]{64}$'::text",
            name="artifact_sha256_valid",
        ),
        CheckConstraint(
            "expires_at > created_at", name="artifact_expiry_after_creation"
        ),
        UniqueConstraint(
            "operation_id", "ordinal", name="uq_artifact_operation_ordinal"
        ),
        Index("idx_artifact_expiry", "expires_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paid_operations.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    mime_type: Mapped[str] = mapped_column(String(127))
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(32), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class DeliveryIntent(TimestampMixin, Base):
    """One persisted Telegram send target and its certainty state."""

    __tablename__ = "delivery_intents"
    __table_args__ = (
        CheckConstraint("chat_id <> 0", name="delivery_chat_nonzero"),
        CheckConstraint(
            "thread_id IS NULL OR thread_id > 0", name="delivery_thread_positive"
        ),
        CheckConstraint(
            "reply_to_message_id IS NULL OR reply_to_message_id > 0",
            name="delivery_reply_positive",
        ),
        CheckConstraint(
            "progress_message_id IS NULL OR progress_message_id > 0",
            name="delivery_progress_positive",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="delivery_attempt_count_non_negative"
        ),
        CheckConstraint(
            "resend_token_hash::text ~ '^[0-9a-f]{64}$'::text",
            name="delivery_resend_token_hash_valid",
        ),
        CheckConstraint(
            "state::text = ANY (ARRAY["
            "'not_ready'::text, 'pending'::text, 'delivering'::text, "
            "'delivered'::text, 'uncertain'::text, 'failed'::text, "
            "'expired'::text])",
            name="delivery_state_allowed",
        ),
        CheckConstraint(
            "jsonb_typeof(telegram_message_ids) = 'array'::text",
            name="delivery_message_ids_array",
        ),
        CheckConstraint(
            "expires_at > created_at", name="delivery_expiry_after_creation"
        ),
        CheckConstraint(
            "state::text = 'delivered'::text AND delivered_at IS NOT NULL "
            "OR state::text <> 'delivered'::text AND delivered_at IS NULL",
            name="delivery_delivered_timestamp_matches",
        ),
        CheckConstraint(
            "state::text = 'uncertain'::text AND uncertain_at IS NOT NULL "
            "OR state::text <> 'uncertain'::text AND uncertain_at IS NULL",
            name="delivery_uncertain_timestamp_matches",
        ),
        CheckConstraint(
            "(state::text = ANY (ARRAY['failed'::text, 'expired'::text])) "
            "AND failed_at IS NOT NULL OR NOT (state::text = ANY "
            "(ARRAY['failed'::text, 'expired'::text])) AND failed_at IS NULL",
            name="delivery_failed_timestamp_matches",
        ),
        Index("idx_delivery_reconciliation", "state", "updated_at"),
        Index("idx_delivery_expiry", "expires_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paid_operations.id", ondelete="CASCADE"), unique=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    business_connection_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resend_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(
        String(16), default="not_ready", server_default=text("'not_ready'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    telegram_message_ids: Mapped[list[int]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    uncertain_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
