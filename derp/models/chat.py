"""Chat model representing Telegram chats with settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from derp.models.credit_transaction import CreditTransaction
    from derp.models.daily_usage import DailyUsage
    from derp.models.message import Message
    from derp.models.shared_fact import SharedFact


class Chat(TimestampMixin, Base):
    """Represents a Telegram chat with associated structured policy."""

    __tablename__ = "chats"
    __table_args__ = (
        CheckConstraint(
            "admin_policy IS NULL OR length(admin_policy) <= 2048",
            name="admin_policy_max_length",
        ),
        CheckConstraint(
            "retention_days = ANY (ARRAY[7, 30, 90])",
            name="chat_retention_days_allowed",
        ),
        CheckConstraint("credits >= 0", name="chat_credits_non_negative"),
        CheckConstraint(
            "free_inference_revision > 0",
            name="chat_free_inference_revision_positive",
        ),
        CheckConstraint(
            "num_nonnulls(free_inference_tos_version, "
            "free_inference_privacy_version, free_inference_accepted_by_user_id, "
            "free_inference_accepted_at) = ANY (ARRAY[0, 4])",
            name="chat_free_inference_acceptance_complete",
        ),
        CheckConstraint(
            "coalesce(length(btrim(free_inference_tos_version::text)), 1) > 0 "
            "AND coalesce(length(btrim(free_inference_privacy_version::text)), 1) "
            "> 0",
            name="chat_free_inference_versions_nonblank",
        ),
        CheckConstraint(
            "free_inference_enabled = false AND "
            "(free_inference_accepted_at IS NULL AND "
            "free_inference_revoked_at IS NULL OR "
            "free_inference_accepted_at IS NOT NULL AND "
            "free_inference_revoked_at IS NOT NULL) OR "
            "free_inference_enabled = true AND "
            "free_inference_accepted_at IS NOT NULL AND "
            "free_inference_revoked_at IS NULL",
            name="chat_free_inference_state_complete",
        ),
        CheckConstraint(
            "free_inference_revoked_at IS NULL OR "
            "free_inference_revoked_at >= free_inference_accepted_at",
            name="chat_free_inference_revocation_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(20))  # private, group, supergroup, channel
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_forum: Mapped[bool] = mapped_column(default=False)

    # Structured chat policy. Factual memory lives in scoped SharedFact rows.
    admin_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    ambient_history_enabled: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    context_notice_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    retention_days: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30")
    )
    member_notice_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    shared_facts_member_edit: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    shared_credit_spending_enabled: Mapped[bool] = mapped_column(
        default=True, server_default=text("true")
    )
    expensive_tools_enabled: Mapped[bool] = mapped_column(
        default=True, server_default=text("true")
    )
    free_inference_enabled: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    free_inference_revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    free_inference_tos_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    free_inference_privacy_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    free_inference_accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    free_inference_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    free_inference_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Credit balance for group pool (sponsors can fund the group)
    credits: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    # Relationships
    messages: Mapped[list[Message]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )
    credit_transactions: Mapped[list[CreditTransaction]] = relationship(
        back_populates="chat"
    )
    daily_usages: Mapped[list[DailyUsage]] = relationship(back_populates="chat")
    shared_facts: Mapped[list[SharedFact]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        """Display name for the chat."""
        if self.title:
            return self.title
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            if self.last_name:
                return f"{self.first_name} {self.last_name}"
            return self.first_name
        return str(self.telegram_id)

    def __repr__(self) -> str:
        return f"<Chat(telegram_id={self.telegram_id}, type={self.type!r}, display_name={self.display_name!r})>"
