"""Immutable quotes and durable paid-operation state."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class OperationQuote(Base):
    """A fixed, expiring price bound to one operation identity."""

    __tablename__ = "operation_quotes"
    __table_args__ = (
        CheckConstraint(
            "amount_credits >= 0", name="operation_quote_amount_non_negative"
        ),
        CheckConstraint(
            "estimated_provider_cost_usd >= 0::numeric",
            name="operation_quote_provider_cost_non_negative",
        ),
        CheckConstraint(
            "expires_at > created_at", name="operation_quote_expiry_after_creation"
        ),
        CheckConstraint(
            "thread_id IS NULL OR thread_id > 0",
            name="operation_quote_thread_positive",
        ),
        CheckConstraint(
            "context_band::text = ANY (ARRAY['small'::text, 'medium'::text, "
            "'large'::text, 'maximum'::text])",
            name="operation_quote_context_band_allowed",
        ),
        UniqueConstraint("id", "operation_id", name="uq_operation_quote_operation"),
        Index("idx_operation_quote_scope", "chat_id", "thread_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    request_key: Mapped[str] = mapped_column(String(255), unique=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="RESTRICT"), index=True
    )
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    feature: Mapped[str] = mapped_column(String(32))
    model_key: Mapped[str] = mapped_column(String(32))
    provider_model_id: Mapped[str] = mapped_column(String(100))
    context_band: Mapped[str] = mapped_column(String(16))
    variant: Mapped[str] = mapped_column(String(64), default="default")
    amount_credits: Mapped[int] = mapped_column(Integer)
    estimated_provider_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    pricing_version: Mapped[str] = mapped_column(String(32))
    catalog_verified_on: Mapped[date] = mapped_column(Date)
    pricing_input: Mapped[dict[str, object]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class PaidOperation(TimestampMixin, Base):
    """State machine for one exactly-once provider side effect and charge."""

    __tablename__ = "paid_operations"
    __table_args__ = (
        CheckConstraint(
            "state::text = ANY (ARRAY["
            "'quoted'::text, 'reserved'::text, 'executing'::text, "
            "'captured'::text, 'released'::text, 'reversed'::text, "
            "'canceled'::text, 'failed'::text])",
            name="paid_operation_state_allowed",
        ),
        CheckConstraint(
            "delivery_state::text = ANY (ARRAY["
            "'not_ready'::text, 'pending'::text, 'delivering'::text, "
            "'delivered'::text, 'uncertain'::text, 'failed'::text, "
            "'expired'::text])",
            name="paid_operation_delivery_state_allowed",
        ),
        CheckConstraint(
            "funding_authorization IS NULL OR "
            "(funding_authorization::text = ANY (ARRAY["
            "'chat'::text, 'private'::text, 'once'::text, 'always'::text]))",
            name="paid_operation_funding_authorization_allowed",
        ),
        ForeignKeyConstraint(
            ["quote_id", "id"],
            ["operation_quotes.id", "operation_quotes.operation_id"],
            name="fk_paid_operation_matching_quote",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "wallet_id", name="uq_paid_operation_wallet"),
        Index("idx_paid_operation_reconciliation", "state", "updated_at"),
        Index(
            "idx_paid_operation_delivery_reconciliation",
            "delivery_state",
            "updated_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    quote_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=True
    )
    funding_authorization: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), default="quoted", server_default=text("'quoted'")
    )
    delivery_state: Mapped[str] = mapped_column(
        String(16), default="not_ready", server_default=text("'not_ready'")
    )
    reserved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_metadata: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class OperationAllocation(Base):
    """The exact inventory lots reserved by one operation."""

    __tablename__ = "operation_allocations"
    __table_args__ = (
        CheckConstraint(
            "amount_credits > 0", name="operation_allocation_amount_positive"
        ),
        ForeignKeyConstraint(
            ["operation_id", "wallet_id"],
            ["paid_operations.id", "paid_operations.wallet_id"],
            name="fk_operation_allocation_operation_wallet",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["wallet_lot_id", "wallet_id"],
            ["wallet_lots.id", "wallet_lots.wallet_id"],
            name="fk_operation_allocation_lot_wallet",
            ondelete="RESTRICT",
        ),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    wallet_lot_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column()
    amount_credits: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class DeferredToolRequest(TimestampMixin, Base):
    """Validated paid tool call waiting for an authenticated decision."""

    __tablename__ = "deferred_tool_requests"
    __table_args__ = (
        CheckConstraint(
            "thread_id IS NULL OR thread_id > 0",
            name="deferred_tool_thread_positive",
        ),
        CheckConstraint("message_id > 0", name="deferred_tool_message_positive"),
        CheckConstraint(
            "status::text = ANY (ARRAY["
            "'pending'::text, 'approved'::text, 'denied'::text, "
            "'expired'::text, 'resumed'::text])",
            name="deferred_tool_status_allowed",
        ),
        CheckConstraint(
            "expires_at > created_at", name="deferred_tool_expiry_after_creation"
        ),
        Index("idx_deferred_tool_expiry", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paid_operations.id", ondelete="CASCADE"), unique=True
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_quotes.id", ondelete="RESTRICT"), unique=True
    )
    callback_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="RESTRICT"), index=True
    )
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_call_id: Mapped[str] = mapped_column(String(255))
    validated_arguments: Mapped[dict[str, object]] = mapped_column(JSONB)
    original_history: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    history_schema_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
