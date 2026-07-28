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
    terms_acceptance_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "legal_acceptances.id",
            name="fk_purchase_intent_terms_acceptance",
            ondelete="RESTRICT",
        ),
        nullable=True,
        index=True,
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
            "'received'::text, 'fulfilled'::text, 'refund_requested'::text, "
            "'clawed_back'::text, 'needs_review'::text])",
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


class PaymentUpdateInbox(TimestampMixin, Base):
    """Content-minimized Telegram payment update awaiting reconciliation."""

    __tablename__ = "payment_update_inbox"
    __table_args__ = (
        CheckConstraint(
            "kind::text = ANY (ARRAY["
            "'successful_payment'::text, 'refunded_payment'::text])",
            name="payment_update_inbox_kind_allowed",
        ),
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'pending'::text, 'processing'::text, 'completed'::text, "
            "'attention'::text])",
            name="payment_update_inbox_status_allowed",
        ),
        CheckConstraint(
            "reply_status::text = ANY (ARRAY["
            "'pending'::text, 'sent'::text, 'failed'::text, 'skipped'::text])",
            name="payment_update_inbox_reply_status_allowed",
        ),
        CheckConstraint(
            "settlement_state IS NULL OR (settlement_state::text = ANY (ARRAY["
            "'fulfilled'::text, 'clawed_back'::text, 'refunded'::text]))",
            name="payment_update_inbox_settlement_state_allowed",
        ),
        CheckConstraint(
            "currency::text = 'XTR'::text",
            name="payment_update_inbox_currency_xtr",
        ),
        CheckConstraint(
            "telegram_update_id >= 0 AND total_amount > 0 "
            "AND attempt_count >= 0 AND reply_attempt_count >= 0",
            name="payment_update_inbox_values_valid",
        ),
        CheckConstraint(
            "status::text = 'processing'::text "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL OR "
            "status::text <> 'processing'::text "
            "AND lease_token IS NULL AND lease_expires_at IS NULL",
            name="payment_update_inbox_lease_matches_status",
        ),
        CheckConstraint(
            "kind::text = 'successful_payment'::text OR "
            "NOT is_recurring AND NOT is_first_recurring "
            "AND subscription_expiration_at IS NULL",
            name="payment_update_inbox_recurring_fields_match_kind",
        ),
        CheckConstraint(
            "NOT is_first_recurring OR is_recurring",
            name="payment_update_inbox_first_recurring_matches",
        ),
        Index(
            "idx_payment_update_inbox_due",
            "status",
            "next_attempt_at",
            "telegram_update_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload_token_hash: Mapped[str] = mapped_column(String(64))
    telegram_charge_id: Mapped[str] = mapped_column(String(255), index=True)
    provider_charge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payer_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_language: Mapped[str] = mapped_column(
        String(8), default="en", server_default=text("'en'")
    )
    currency: Mapped[str] = mapped_column(String(3))
    total_amount: Mapped[int] = mapped_column(Integer)
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
        String(16), default="pending", server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    reply_attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settlement_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attention_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reply_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    replied_at: Mapped[datetime | None] = mapped_column(
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


class SubscriptionRenewalCommandRecord(TimestampMixin, Base):
    """One leased, restart-safe request to change Telegram renewal state."""

    __tablename__ = "subscription_renewal_commands"
    __table_args__ = (
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'pending'::text, 'processing'::text, 'applied'::text, "
            "'superseded'::text, 'attention'::text])",
            name="subscription_renewal_command_status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="subscription_renewal_command_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "payer_telegram_id > 0 AND length(btrim(telegram_charge_id::text)) > 0",
            name="subscription_renewal_command_provider_identity_valid",
        ),
        CheckConstraint(
            "status::text = 'processing'::text "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL OR "
            "status::text <> 'processing'::text "
            "AND lease_token IS NULL AND lease_expires_at IS NULL",
            name="subscription_renewal_command_lease_matches_status",
        ),
        CheckConstraint(
            "(status::text = ANY (ARRAY['applied'::text, 'superseded'::text])) "
            "AND completed_at IS NOT NULL OR "
            "(status::text <> ALL (ARRAY['applied'::text, 'superseded'::text])) "
            "AND completed_at IS NULL",
            name="subscription_renewal_command_completion_matches_status",
        ),
        Index(
            "uq_subscription_renewal_command_active",
            "subscription_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'processing', 'attention')"),
        ),
        Index(
            "idx_subscription_renewal_command_due",
            "status",
            "next_attempt_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), index=True
    )
    payer_telegram_id: Mapped[int] = mapped_column(BigInteger)
    telegram_charge_id: Mapped[str] = mapped_column(String(255))
    desired_enabled: Mapped[bool]
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
