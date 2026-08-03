"""Immutable legal acceptances and bounded support workflows."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class LegalAcceptance(Base):
    """One immutable acceptance of a published legal document version."""

    __tablename__ = "legal_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "document",
            "version",
            name="legal_acceptance_user_document_version_unique",
        ),
        CheckConstraint(
            "document::text = 'terms'::text",
            name="legal_acceptance_document_allowed",
        ),
        CheckConstraint(
            "source::text = ANY (ARRAY['terms_command'::text, 'purchase_gate'::text])",
            name="legal_acceptance_source_allowed",
        ),
        CheckConstraint(
            "length(btrim(version::text)) > 0",
            name="legal_acceptance_version_nonblank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SupportRequest(TimestampMixin, Base):
    """One bounded support case with an optional exact payment relation."""

    __tablename__ = "support_requests"
    __table_args__ = (
        UniqueConstraint("reference", name="support_request_reference_unique"),
        CheckConstraint(
            "kind::text = ANY "
            "(ARRAY['payment'::text, 'refund'::text, 'privacy'::text, "
            "'access'::text])",
            name="support_request_kind_allowed",
        ),
        CheckConstraint(
            "source::text = ANY "
            "(ARRAY['support'::text, 'paysupport'::text, 'privacy'::text])",
            name="support_request_source_allowed",
        ),
        CheckConstraint(
            "status::text = ANY (ARRAY['open'::text, 'refund_pending'::text, "
            "'resolved'::text, 'declined'::text])",
            name="support_request_status_allowed",
        ),
        CheckConstraint(
            "(status::text = ANY (ARRAY['open'::text, 'refund_pending'::text])) "
            "AND resolved_at IS NULL OR (status::text = ANY "
            "(ARRAY['resolved'::text, 'declined'::text])) "
            "AND resolved_at IS NOT NULL",
            name="support_request_resolution_complete",
        ),
        CheckConstraint(
            "description IS NULL OR char_length(btrim(description)) >= 1 "
            "AND char_length(btrim(description)) <= 800",
            name="support_request_description_bounded",
        ),
        CheckConstraint(
            "decision_reason IS NULL OR char_length(btrim(decision_reason)) >= 1 "
            "AND char_length(btrim(decision_reason)) <= 800",
            name="support_request_decision_reason_bounded",
        ),
        CheckConstraint(
            "num_nonnulls(status_message_chat_id, status_message_id) "
            "= ANY (ARRAY[0, 2])",
            name="support_request_status_message_complete",
        ),
        CheckConstraint(
            "payment_receipt_id IS NULL OR (kind::text = ANY "
            "(ARRAY['payment'::text, 'refund'::text]))",
            name="support_request_payment_kind_matches",
        ),
        Index(
            "support_request_one_open_kind_per_user",
            "requester_user_id",
            "kind",
            unique=True,
            postgresql_where=text("status IN ('open', 'refund_pending')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_receipts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default=text("'open'")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    operator_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status_message_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    status_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SupportIntake(Base):
    """Short-lived durable context behind one native ForceReply prompt."""

    __tablename__ = "support_intakes"
    __table_args__ = (
        CheckConstraint(
            "kind::text = ANY "
            "(ARRAY['payment'::text, 'refund'::text, 'privacy'::text, "
            "'access'::text])",
            name="support_intake_kind_allowed",
        ),
        CheckConstraint(
            "source::text = ANY "
            "(ARRAY['support'::text, 'paysupport'::text, 'privacy'::text])",
            name="support_intake_source_allowed",
        ),
        CheckConstraint(
            "prompt_chat_id <> 0 AND prompt_message_id > 0",
            name="support_intake_prompt_valid",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="support_intake_expiry_after_creation",
        ),
        CheckConstraint(
            "payment_receipt_id IS NULL OR (kind::text = ANY "
            "(ARRAY['payment'::text, 'refund'::text]))",
            name="support_intake_payment_kind_matches",
        ),
        UniqueConstraint(
            "prompt_chat_id",
            "prompt_message_id",
            name="support_intake_prompt_unique",
        ),
        Index("idx_support_intake_expiry", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_receipts.id", ondelete="RESTRICT"), nullable=True
    )
    prompt_chat_id: Mapped[int] = mapped_column(BigInteger)
    prompt_message_id: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
