"""Chat model representing Telegram chats with settings."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from derp.models.credit_transaction import CreditTransaction
    from derp.models.daily_usage import DailyUsage
    from derp.models.message import Message


class Chat(TimestampMixin, Base):
    """Represents a Telegram chat with associated settings.

    Combines the chat entity with chat settings (llm_memory) for simplicity.
    Supports private chats, groups, supergroups, and channels.
    """

    __tablename__ = "chats"
    __table_args__ = (
        CheckConstraint("length(llm_memory) <= 1024", name="llm_memory_max_length"),
        CheckConstraint("credits >= 0", name="chat_credits_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(20))  # private, group, supergroup, channel
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_forum: Mapped[bool] = mapped_column(default=False)

    # Chat settings (merged from ChatSettings)
    llm_memory: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Credit balance for group pool (sponsors can fund the group)
    credits: Mapped[int] = mapped_column(default=0)

    # Relationships
    messages: Mapped[list[Message]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )
    credit_transactions: Mapped[list[CreditTransaction]] = relationship(
        back_populates="chat"
    )
    daily_usages: Mapped[list[DailyUsage]] = relationship(back_populates="chat")

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
