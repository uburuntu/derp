"""User model representing Telegram users."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from derp.models.credit_transaction import CreditTransaction
    from derp.models.daily_usage import DailyUsage
    from derp.models.message import Message


class User(TimestampMixin, Base):
    """Represents a Telegram user or bot.

    Stores core user information from Telegram with computed properties
    for display purposes.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("credits >= 0", name="user_credits_non_negative"),
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
    credits: Mapped[int] = mapped_column(default=0)

    # Relationships
    messages: Mapped[list[Message]] = relationship(back_populates="user")
    credit_transactions: Mapped[list[CreditTransaction]] = relationship(
        back_populates="user"
    )
    daily_usages: Mapped[list[DailyUsage]] = relationship(back_populates="user")

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
