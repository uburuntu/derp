"""Durable Telegram Stars purchases and subscription cycles."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class PurchaseIntent(Base):
    """Immutable commercial terms behind one opaque invoice payload."""

    __tablename__ = "purchase_intents"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(target_user_id, target_chat_id) = 1",
            name="purchase_intent_exactly_one_target",
        ),
        CheckConstraint(
            "product_kind::text = ANY (ARRAY['top_up'::text, 'subscription'::text])",
            name="purchase_intent_product_kind_allowed",
        ),
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'pending'::text, 'prechecked'::text, 'fulfilled'::text, "
            "'expired'::text, 'canceled'::text, 'needs_review'::text])",
            name="purchase_intent_status_allowed",
        ),
        CheckConstraint(
            "credits > 0 AND stars > 0", name="purchase_intent_amounts_positive"
        ),
        CheckConstraint(
            "currency::text = 'XTR'::text", name="purchase_intent_currency_xtr"
        ),
        CheckConstraint(
            "expires_at > created_at", name="purchase_intent_expiry_after_creation"
        ),
        CheckConstraint(
            "product_kind::text = 'subscription'::text "
            "AND subscription_period_seconds = 2592000 "
            "OR product_kind::text = 'top_up'::text "
            "AND subscription_period_seconds IS NULL",
            name="purchase_intent_subscription_period_matches",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payer_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    target_chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chats.id", ondelete="RESTRICT"), nullable=True
    )
    product_kind: Mapped[str] = mapped_column(String(16))
    product_id: Mapped[str] = mapped_column(String(64))
    product_version: Mapped[str] = mapped_column(String(32))
    credits: Mapped[int] = mapped_column(Integer)
    stars: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="XTR")
    subscription_period_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class PaymentReceipt(TimestampMixin, Base):
    """Idempotent inbox record for every captured Telegram payment."""

    __tablename__ = "payment_receipts"
    __table_args__ = (
        CheckConstraint(
            "currency::text = 'XTR'::text", name="payment_receipt_currency_xtr"
        ),
        CheckConstraint("total_amount > 0", name="payment_receipt_amount_positive"),
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'received'::text, 'fulfilled'::text, 'clawed_back'::text, "
            "'needs_review'::text])",
            name="payment_receipt_status_allowed",
        ),
        CheckConstraint(
            "subscription_expiration_at IS NULL OR is_recurring",
            name="payment_receipt_subscription_fields_match",
        ),
        CheckConstraint(
            "NOT is_first_recurring OR is_recurring",
            name="payment_receipt_first_recurring_matches",
        ),
        Index(
            "idx_payment_receipt_intent_created",
            "purchase_intent_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_intents.id", ondelete="RESTRICT"), nullable=True
    )
    telegram_charge_id: Mapped[str] = mapped_column(String(255), unique=True)
    provider_charge_id: Mapped[str] = mapped_column(String(255))
    payer_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    currency: Mapped[str] = mapped_column(String(3))
    total_amount: Mapped[int] = mapped_column(Integer)
    payload_token_hash: Mapped[str] = mapped_column(String(64), index=True)
    is_recurring: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    is_first_recurring: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    subscription_expiration_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="received", server_default=text("'received'")
    )
    clawed_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Subscription(TimestampMixin, Base):
    """The one recurring personal plan allowed for a user."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status::text = ANY "
            "(ARRAY['active'::text, 'canceled'::text, 'expired'::text])",
            name="subscription_status_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    plan_id: Mapped[str] = mapped_column(String(64))
    plan_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default=text("'active'")
    )
    renewal_enabled: Mapped[bool] = mapped_column(
        default=True, server_default=text("true")
    )
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SubscriptionCycle(Base):
    """One non-rolling allowance grant anchored to a captured payment."""

    __tablename__ = "subscription_cycles"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "period_end",
            name="uq_subscription_cycle_period_end",
        ),
        CheckConstraint(
            "period_end > period_start", name="subscription_cycle_period_order"
        ),
        CheckConstraint(
            "allowance_credits > 0", name="subscription_cycle_allowance_positive"
        ),
        CheckConstraint(
            "status::text = ANY "
            "(ARRAY['active'::text, 'expired'::text, 'clawed_back'::text])",
            name="subscription_cycle_status_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), index=True
    )
    payment_receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment_receipts.id", ondelete="RESTRICT"), unique=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    allowance_credits: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
