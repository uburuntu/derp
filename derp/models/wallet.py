"""Wallet inventories and append-only settlement records."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from derp.models.base import Base, TimestampMixin


class Wallet(TimestampMixin, Base):
    """One personal or shared balance owner, never both."""

    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(user_id, chat_id) = 1",
            name="wallet_exactly_one_owner",
        ),
        CheckConstraint("debt_credits >= 0", name="wallet_debt_non_negative"),
        Index(
            "uq_wallet_user",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_wallet_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("chat_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), nullable=True
    )
    debt_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )


class WalletLot(TimestampMixin, Base):
    """A source-specific inventory whose accounting always reconciles."""

    __tablename__ = "wallet_lots"
    __table_args__ = (
        CheckConstraint(
            "kind::text = ANY (ARRAY['allowance'::text, 'purchased'::text])",
            name="wallet_lot_kind_allowed",
        ),
        CheckConstraint(
            "granted_credits >= 0 AND available_credits >= 0 "
            "AND reserved_credits >= 0 AND consumed_credits >= 0 "
            "AND expired_credits >= 0 AND clawed_back_credits >= 0 "
            "AND debt_offset_credits >= 0",
            name="wallet_lot_counters_non_negative",
        ),
        CheckConstraint(
            "(available_credits + reserved_credits + consumed_credits "
            "+ expired_credits + clawed_back_credits + debt_offset_credits"
            ") = granted_credits",
            name="wallet_lot_accounting_balanced",
        ),
        CheckConstraint(
            "num_nonnulls(payment_receipt_id, subscription_cycle_id) <= 1",
            name="wallet_lot_one_source",
        ),
        CheckConstraint(
            "kind::text = 'allowance'::text "
            "AND subscription_cycle_id IS NOT NULL AND expires_at IS NOT NULL "
            "OR kind::text = 'purchased'::text "
            "AND subscription_cycle_id IS NULL AND expires_at IS NULL",
            name="wallet_lot_kind_source_matches",
        ),
        Index(
            "uq_wallet_lot_payment_receipt",
            "payment_receipt_id",
            unique=True,
            postgresql_where=text("payment_receipt_id IS NOT NULL"),
        ),
        Index(
            "uq_wallet_lot_subscription_cycle",
            "subscription_cycle_id",
            unique=True,
            postgresql_where=text("subscription_cycle_id IS NOT NULL"),
        ),
        UniqueConstraint("id", "wallet_id", name="uq_wallet_lot_wallet"),
        Index(
            "idx_wallet_lots_reservation_order",
            "wallet_id",
            "kind",
            "expires_at",
            "created_at",
            postgresql_where=text("available_credits > 0"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    payment_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_receipts.id", ondelete="RESTRICT"), nullable=True
    )
    subscription_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscription_cycles.id", ondelete="RESTRICT"), nullable=True
    )
    granted_credits: Mapped[int] = mapped_column(Integer)
    available_credits: Mapped[int] = mapped_column(Integer)
    reserved_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    consumed_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    expired_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    clawed_back_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    debt_offset_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WalletDebtSource(TimestampMixin, Base):
    """Debt created by revoking one exact wallet-lot source."""

    __tablename__ = "wallet_debt_sources"
    __table_args__ = (
        CheckConstraint(
            "incurred_credits > 0",
            name="wallet_debt_source_incurred_positive",
        ),
        CheckConstraint(
            "outstanding_credits >= 0 AND recovered_credits >= 0 "
            "AND (outstanding_credits + recovered_credits) <= incurred_credits",
            name="wallet_debt_source_counters_valid",
        ),
        ForeignKeyConstraint(
            ["source_wallet_lot_id", "wallet_id"],
            ["wallet_lots.id", "wallet_lots.wallet_id"],
            name="fk_wallet_debt_source_lot_wallet",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "source_wallet_lot_id",
            name="uq_wallet_debt_source_lot",
        ),
        UniqueConstraint("id", "wallet_id", name="uq_wallet_debt_source_wallet"),
        Index(
            "idx_wallet_debt_sources_open",
            "wallet_id",
            "created_at",
            "id",
            postgresql_where=text("outstanding_credits > 0"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT")
    )
    source_wallet_lot_id: Mapped[uuid.UUID] = mapped_column()
    incurred_credits: Mapped[int] = mapped_column(Integer)
    outstanding_credits: Mapped[int] = mapped_column(Integer)
    recovered_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )


class WalletDebtRepaymentAllocation(TimestampMixin, Base):
    """Exact grant-lot value applied to one debt source."""

    __tablename__ = "wallet_debt_repayment_allocations"
    __table_args__ = (
        CheckConstraint(
            "allocated_credits > 0",
            name="wallet_debt_repayment_allocated_positive",
        ),
        CheckConstraint(
            "restored_credits >= 0 AND revoked_credits >= 0 "
            "AND (restored_credits + revoked_credits) <= allocated_credits",
            name="wallet_debt_repayment_counters_valid",
        ),
        ForeignKeyConstraint(
            ["debt_source_id", "wallet_id"],
            ["wallet_debt_sources.id", "wallet_debt_sources.wallet_id"],
            name="fk_wallet_debt_repayment_source_wallet",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["repayment_wallet_lot_id", "wallet_id"],
            ["wallet_lots.id", "wallet_lots.wallet_id"],
            name="fk_wallet_debt_repayment_lot_wallet",
            ondelete="RESTRICT",
        ),
        Index(
            "idx_wallet_debt_repayments_active",
            "debt_source_id",
            "created_at",
            "repayment_wallet_lot_id",
            postgresql_where=text(
                "restored_credits + revoked_credits < allocated_credits"
            ),
        ),
    )

    debt_source_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    repayment_wallet_lot_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column()
    allocated_credits: Mapped[int] = mapped_column(Integer)
    restored_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    revoked_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )


class PersonalSpendConsent(TimestampMixin, Base):
    """A user's durable permission to use their wallet in one chat."""

    __tablename__ = "personal_spend_consents"
    __table_args__ = (
        CheckConstraint(
            "enabled AND revoked_at IS NULL OR NOT enabled AND revoked_at IS NOT NULL",
            name="personal_spend_consent_state_complete",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WalletLedgerEntry(Base):
    """Append-only explanation of one non-negative wallet movement."""

    __tablename__ = "wallet_ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_credits > 0", name="wallet_ledger_amount_positive"),
        CheckConstraint(
            "available_after >= 0 AND reserved_after >= 0 "
            "AND consumed_after >= 0 AND wallet_debt_after >= 0",
            name="wallet_ledger_snapshots_non_negative",
        ),
        CheckConstraint(
            "event_type::text = ANY (ARRAY["
            "'grant'::text, 'reserve'::text, 'capture'::text, "
            "'release'::text, 'reversal'::text, 'expire'::text, "
            "'clawback'::text, 'debt_incurred'::text, 'debt_offset'::text, "
            "'debt_restored'::text, 'debt_reopened'::text])",
            name="wallet_ledger_event_allowed",
        ),
        ForeignKeyConstraint(
            ["wallet_lot_id", "wallet_id"],
            ["wallet_lots.id", "wallet_lots.wallet_id"],
            name="fk_wallet_ledger_lot_wallet",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "wallet_id"],
            ["paid_operations.id", "paid_operations.wallet_id"],
            name="fk_wallet_ledger_operation_wallet",
            ondelete="RESTRICT",
        ),
        Index("idx_wallet_ledger_created", "wallet_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT")
    )
    wallet_lot_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    payment_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_receipts.id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    amount_credits: Mapped[int] = mapped_column(Integer)
    available_after: Mapped[int] = mapped_column(Integer)
    reserved_after: Mapped[int] = mapped_column(Integer)
    consumed_after: Mapped[int] = mapped_column(Integer)
    wallet_debt_after: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
