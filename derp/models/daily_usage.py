"""Daily usage tracking for free tier limits."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base

if TYPE_CHECKING:
    from derp.models.chat import Chat
    from derp.models.user import User


class DailyUsage(Base):
    """Track free tier consumption per user+chat per day.

    Stores per-tool usage counts in a JSONB column for flexibility.
    The usage is reset daily (tracked by usage_date).
    """

    __tablename__ = "daily_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", "usage_date", name="uq_daily_usage"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Who used the features
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)

    # In which chat
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)

    # The day (UTC)
    usage_date: Mapped[date] = mapped_column(Date, index=True)

    # Per-tool usage counts: {"web_search": 3, "image_generate": 1}
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    user: Mapped[User] = relationship(back_populates="daily_usages")
    chat: Mapped[Chat] = relationship(back_populates="daily_usages")

    def get_usage(self, tool_name: str) -> int:
        """Get usage count for a specific tool."""
        return self.usage.get(tool_name, 0)

    def __repr__(self) -> str:
        return (
            f"<DailyUsage(date={self.usage_date}, user_id={self.user_id}, "
            f"chat_id={self.chat_id}, usage={self.usage})>"
        )
