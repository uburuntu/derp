"""Content-free Telegram delivery metadata for inspectable AI runs."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base


class ChatRunReceipt(Base):
    """One chat run mapped to the Telegram messages that delivered its answer."""

    __tablename__ = "chat_run_receipts"
    __table_args__ = (
        CheckConstraint(
            "request_message_id > 0",
            name="chat_run_receipt_request_message_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(response_message_ids) = 'array'::text "
            "AND jsonb_array_length(response_message_ids) > 0",
            name="chat_run_receipt_response_messages_nonempty",
        ),
        CheckConstraint(
            "privacy_mode::text = ANY (ARRAY['private'::text, 'free'::text])",
            name="chat_run_receipt_privacy_mode_allowed",
        ),
        CheckConstraint(
            "context_messages >= 0 AND context_turns >= 0 "
            "AND context_estimated_tokens >= 0",
            name="chat_run_receipt_context_nonnegative",
        ),
        Index("idx_chat_run_receipt_chat", "chat_id", "created_at"),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paid_operations.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    request_message_id: Mapped[int] = mapped_column(BigInteger)
    response_message_ids: Mapped[list[int]] = mapped_column(JSONB)
    model_key: Mapped[str] = mapped_column(String(32))
    model_display_name: Mapped[str] = mapped_column(String(255))
    privacy_mode: Mapped[str] = mapped_column(String(16))
    context_messages: Mapped[int] = mapped_column(Integer)
    context_turns: Mapped[int] = mapped_column(Integer)
    context_estimated_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


__all__ = ["ChatRunReceipt"]
