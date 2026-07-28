"""Immutable legal acceptances and content-free support cases."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
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
    """A bounded support signal without user-authored content."""

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
            "status::text = ANY (ARRAY['open'::text, 'resolved'::text])",
            name="support_request_status_allowed",
        ),
        CheckConstraint(
            "status::text = 'open'::text AND resolved_at IS NULL OR "
            "status::text = 'resolved'::text AND resolved_at IS NOT NULL",
            name="support_request_resolution_complete",
        ),
        Index(
            "support_request_one_open_kind_per_user",
            "requester_user_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default=text("'open'")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
