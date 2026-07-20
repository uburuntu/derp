"""Message model for conversation history (replaces MessageLog)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
        CheckConstraint(
            "role::text = ANY (ARRAY['user'::text, 'assistant'::text])",
            name="message_role_allowed",
        ),
        CheckConstraint(
            "capture_kind::text = ANY (ARRAY['ambient'::text, 'explicit'::text])",
            name="message_capture_kind_allowed",
        ),
        CheckConstraint(
            "source_schema_version >= 1 AND history_schema_version >= 1 "
            "AND canonical_schema_version >= 1",
            name="message_projection_versions_positive",
        ),
        CheckConstraint(
            "privacy_deleted_at IS NULL OR "
            "user_id IS NULL AND content_type::text = 'deleted'::text "
            "AND text = '[message deleted]'::text "
            "AND attachment_type IS NULL AND attachment_file_id IS NULL",
            name="message_tombstone_anonymous",
        ),
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
        Index(
            "idx_messages_scope_history",
            "chat_id",
            "thread_id",
            "telegram_date",
            "telegram_message_id",
            postgresql_where="deleted_at IS NULL",
        ),
        # Index for user message history
        Index("idx_messages_user", "user_id", postgresql_where="user_id IS NOT NULL"),
        # Index for telegram_date for time-based queries
        Index("idx_messages_telegram_date", "chat_id", "telegram_date"),
        Index("idx_messages_retention_expiry", "retention_expires_at"),
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
    role: Mapped[str] = mapped_column(
        String(16), default="user", server_default=sql_text("'user'")
    )
    capture_kind: Mapped[str] = mapped_column(
        String(16), default="explicit", server_default=sql_text("'explicit'")
    )

    # Versioned source, application-history, and canonical projections.
    source_schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=sql_text("1")
    )
    source_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    history_schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=sql_text("1")
    )
    history_dto: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    canonical_schema_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=sql_text("1")
    )
    canonical_projection: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )

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
    privacy_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC) + timedelta(days=30),
        server_default=sql_text("(now() + '30 days'::interval)"),
    )

    # Relationships
    chat: Mapped[Chat] = relationship(back_populates="messages")
    user: Mapped[User | None] = relationship(back_populates="messages")

    @property
    def is_deleted(self) -> bool:
        """Check if the message has been marked as deleted."""
        return self.deleted_at is not None

    @property
    def is_tombstone(self) -> bool:
        """Return whether application-managed content has been anonymized."""
        return self.privacy_deleted_at is not None

    @property
    def message_key(self) -> str:
        """Generate the natural key for this message."""
        return f"{self.chat_id}:{self.thread_id or 0}:{self.telegram_message_id}"

    def __repr__(self) -> str:
        return (
            f"<Message(id={self.id}, chat_id={self.chat_id}, "
            f"telegram_message_id={self.telegram_message_id}, direction={self.direction!r})>"
        )
