"""Versioned acknowledgements for concise one-time product notices."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base


class UserNotice(Base):
    """The latest version of one notice presented to a user."""

    __tablename__ = "user_notices"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(notice::text)) > 0",
            name="user_notice_name_nonblank",
        ),
        CheckConstraint("version > 0", name="user_notice_version_positive"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    notice: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    shown_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


__all__ = ["UserNotice"]
