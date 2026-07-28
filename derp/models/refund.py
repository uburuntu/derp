"""Durable outbound Telegram Stars refund commands."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class PaymentRefundRequest(TimestampMixin, Base):
    """One restart-safe provider refund command for a captured payment."""

    __tablename__ = "payment_refund_requests"
    __table_args__ = (
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'pending'::text, 'submitting'::text, 'accepted'::text, "
            "'rejected'::text, 'needs_review'::text, 'reconciled'::text])",
            name="payment_refund_request_status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="payment_refund_request_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "status::text = 'pending'::text AND submitted_at IS NULL OR "
            "status::text <> 'pending'::text AND submitted_at IS NOT NULL "
            "AND attempt_count > 0",
            name="payment_refund_request_submission_matches_status",
        ),
        CheckConstraint(
            "(status::text = ANY (ARRAY['accepted'::text, 'reconciled'::text])) "
            "AND accepted_at IS NOT NULL OR "
            "(status::text <> ALL (ARRAY['accepted'::text, 'reconciled'::text])) "
            "AND accepted_at IS NULL",
            name="payment_refund_request_acceptance_matches_status",
        ),
        CheckConstraint(
            "status::text = 'reconciled'::text AND reconciled_at IS NOT NULL OR "
            "status::text <> 'reconciled'::text AND reconciled_at IS NULL",
            name="payment_refund_request_reconciliation_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment_receipts.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    requester_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = ["PaymentRefundRequest"]
