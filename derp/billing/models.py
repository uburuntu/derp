"""Receipts for Stars payments, kept for accounting (never deleted)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base


class Purchase(Base):
    """One successful Stars payment: a pack, or one paid cycle of the subscription."""

    __tablename__ = "purchases"
    __table_args__ = (
        CheckConstraint("kind in ('pack', 'plan')", name="kind"),
        CheckConstraint("status in ('paid', 'refunded')", name="status"),
        CheckConstraint("stars > 0 and credits > 0", name="amounts_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_charge_id: Mapped[str] = mapped_column(String(128), unique=True)
    provider_charge_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[int]  # who paid
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))  # who got the credits
    kind: Mapped[str] = mapped_column(String(4))
    product: Mapped[str] = mapped_column(String(32))  # versioned pack / plan id
    stars: Mapped[int]
    credits: Mapped[int]
    payload: Mapped[str] = mapped_column(String(128))
    is_recurring: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime | None]  # subscription cycle end
    status: Mapped[str] = mapped_column(String(8), default="paid")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    refunded_at: Mapped[datetime | None]


class Subscription(Base):
    """A user's Derp+ subscription, identified by the invoice payload it was bought with."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("state in ('active', 'canceled', 'failed')", name="state"),
        Index("ix_subscriptions_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[int]
    plan: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(8), default="active")  # canceled: runs until period_end
    first_charge_id: Mapped[str] = mapped_column(String(128))  # what editUserStarSubscription takes
    last_charge_id: Mapped[str] = mapped_column(String(128))
    period_end: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
