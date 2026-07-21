"""Durable per-user allowance for bounded free inline answers."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class InlineDailyAllowance(TimestampMixin, Base):
    """Count provider attempts claimed by one user during one UTC day."""

    __tablename__ = "inline_daily_allowances"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "usage_date",
            name="uq_inline_daily_allowance_user_date",
        ),
        CheckConstraint(
            "used_count >= 0",
            name="inline_daily_allowance_used_non_negative",
        ),
        Index("ix_inline_daily_allowances_usage_date", "usage_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False)


__all__ = ["InlineDailyAllowance"]
