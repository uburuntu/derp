"""User model representing Telegram users."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from derp.models.credit_transaction import CreditTransaction
    from derp.models.daily_usage import DailyUsage
    from derp.models.message import Message
    from derp.models.shared_fact import SharedFact


class User(TimestampMixin, Base):
    """Represents a Telegram user or bot.

    Stores core user information from Telegram with computed properties
    for display purposes.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("credits >= 0", name="user_credits_non_negative"),
        CheckConstraint(
            "inference_privacy_mode::text = ANY "
            "(ARRAY['private_only'::text, 'allow_non_zdr_free'::text])",
            name="user_inference_privacy_mode_allowed",
        ),
        CheckConstraint(
            "inference_privacy_revision > 0",
            name="user_inference_privacy_revision_positive",
        ),
        CheckConstraint(
            "num_nonnulls(free_inference_tos_version, "
            "free_inference_privacy_version, free_inference_accepted_at) "
            "= ANY (ARRAY[0, 3])",
            name="user_free_inference_acceptance_complete",
        ),
        CheckConstraint(
            "coalesce(length(btrim(free_inference_tos_version::text)), 1) > 0 "
            "AND coalesce(length(btrim(free_inference_privacy_version::text)), 1) "
            "> 0",
            name="user_free_inference_versions_nonblank",
        ),
        CheckConstraint(
            "inference_privacy_mode::text = 'private_only'::text AND "
            "(free_inference_accepted_at IS NULL AND "
            "free_inference_revoked_at IS NULL OR "
            "free_inference_accepted_at IS NOT NULL AND "
            "free_inference_revoked_at IS NOT NULL) OR "
            "inference_privacy_mode::text = 'allow_non_zdr_free'::text AND "
            "free_inference_accepted_at IS NOT NULL AND "
            "free_inference_revoked_at IS NULL",
            name="user_free_inference_state_complete",
        ),
        CheckConstraint(
            "free_inference_revoked_at IS NULL OR "
            "free_inference_revoked_at >= free_inference_accepted_at",
            name="user_free_inference_revocation_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    is_bot: Mapped[bool] = mapped_column(default=False)
    first_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_premium: Mapped[bool] = mapped_column(default=False)

    # Credit balance for paid features
    credits: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    # Non-ZDR free inference remains opt-in and private/inline-only at policy time.
    inference_privacy_mode: Mapped[str] = mapped_column(
        String(32),
        default="private_only",
        server_default=text("'private_only'"),
    )
    inference_privacy_revision: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    free_inference_tos_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    free_inference_privacy_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    free_inference_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    free_inference_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    messages: Mapped[list[Message]] = relationship(back_populates="user")
    credit_transactions: Mapped[list[CreditTransaction]] = relationship(
        back_populates="user"
    )
    daily_usages: Mapped[list[DailyUsage]] = relationship(back_populates="user")
    proposed_shared_facts: Mapped[list[SharedFact]] = relationship(
        back_populates="proposer",
        foreign_keys="SharedFact.proposed_by_user_id",
    )
    decided_shared_facts: Mapped[list[SharedFact]] = relationship(
        back_populates="decider",
        foreign_keys="SharedFact.decided_by_user_id",
    )

    @property
    def full_name(self) -> str:
        """Full name combining first and last name."""
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name

    @property
    def display_name(self) -> str:
        """Display name preferring username over full name."""
        if self.username:
            return f"@{self.username}"
        return self.full_name

    def __repr__(self) -> str:
        return f"<User(telegram_id={self.telegram_id}, display_name={self.display_name!r})>"
