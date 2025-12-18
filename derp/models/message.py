"""Message model for conversation history (replaces MessageLog)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from derp.models.chat import Chat
    from derp.models.user import User


class Message(TimestampMixin, Base):
    """Represents a message in the conversation history.

    Tracks both inbound (from users) and outbound (from bot) messages
    for building LLM context. Replaces the old MessageLog table.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Natural key for upserts: chat + message_id (Telegram message_ids are unique per chat)
        UniqueConstraint(
            "chat_id",
            "telegram_message_id",
            name="uq_messages_chat_message",
        ),
        # Index for efficient context queries (recent messages per chat, non-deleted)
        Index(
            "idx_messages_chat_recent",
            "chat_id",
            "created_at",
            "telegram_message_id",
            postgresql_where="deleted_at IS NULL",
        ),
        # Index for user message history
        Index("idx_messages_user", "user_id", postgresql_where="user_id IS NOT NULL"),
        # Index for telegram_date for time-based queries
        Index("idx_messages_telegram_date", "chat_id", "telegram_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Foreign keys
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Telegram identifiers
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Message direction: 'in' for inbound, 'out' for outbound bot messages
    direction: Mapped[str] = mapped_column(String(3))  # 'in' or 'out'

    # Content
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    attachment_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Reply threading
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Timestamps from Telegram
    telegram_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    chat: Mapped[Chat] = relationship(back_populates="messages")
    user: Mapped[User | None] = relationship(back_populates="messages")

    @property
    def is_deleted(self) -> bool:
        """Check if the message has been marked as deleted."""
        return self.deleted_at is not None

    @property
    def message_key(self) -> str:
        """Generate the natural key for this message."""
        return f"{self.chat_id}:{self.thread_id or 0}:{self.telegram_message_id}"

    def __repr__(self) -> str:
        return (
            f"<Message(id={self.id}, chat_id={self.chat_id}, "
            f"telegram_message_id={self.telegram_message_id}, direction={self.direction!r})>"
        )
