"""Wallets, credit lots, reservations and the append-only ledger.

A wallet's balance is the sum of its unexpired lots' ``remaining``; lots are
the only mutable money state and a CHECK keeps them within ``0..amount``.
Every change to a lot is mirrored by a ledger entry keyed by the operation
that caused it.
"""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

WALLET_KINDS = ("user", "chat")
LOT_SOURCES = ("plan", "purchase", "sponsored", "grant", "migration")
ENTRY_KINDS = ("credit", "hold", "capture", "release", "expire", "clawback")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in (" + ", ".join(f"'{v}'" for v in values) + ")"


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint(_in("kind", WALLET_KINDS), name="kind"),
        UniqueConstraint("kind", "owner_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    owner_id: Mapped[int]  # user id or chat id
    # A chat wallet that ran dry was announced once; cleared when it is topped up again.
    empty_noticed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CreditLot(Base):
    """Credits from one source: a purchase, a plan cycle, a day of sponsorship, a grant."""

    __tablename__ = "credit_lots"
    __table_args__ = (
        CheckConstraint(_in("source", LOT_SOURCES), name="source"),
        CheckConstraint("amount > 0 and remaining >= 0 and remaining <= amount", name="remaining_in_range"),
        Index("ix_credit_lots_wallet_id_expires_at", "wallet_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    operation_id: Mapped[str] = mapped_column(String(512), unique=True)
    source: Mapped[str] = mapped_column(String(10))
    amount: Mapped[int]
    remaining: Mapped[int]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]  # clawed back (refunded): credits returned to it are clawed back too
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Reservation(Base):
    """Credits held for one operation until it is captured (actual cost) or released."""

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("state in ('held', 'captured', 'released')", name="state"),
        CheckConstraint("amount > 0 and captured >= 0 and captured <= amount", name="captured_in_range"),
        Index("ix_reservations_state_created_at", "state", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    operation_id: Mapped[str] = mapped_column(String(512), unique=True)
    amount: Mapped[int]
    captured: Mapped[int] = mapped_column(default=0)
    state: Mapped[str] = mapped_column(String(10), default="held")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    settled_at: Mapped[datetime | None]


class Hold(Base):
    """The part of a reservation taken from one lot."""

    __tablename__ = "reservation_holds"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"), primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("credit_lots.id"), primary_key=True)
    amount: Mapped[int]


class LedgerEntry(Base):
    """One movement on one lot; never updated or deleted."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(_in("kind", ENTRY_KINDS), name="kind"),
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint("operation_id", "kind", "lot_id"),
        Index("ix_ledger_entries_wallet_id_created_at", "wallet_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    lot_id: Mapped[int] = mapped_column(ForeignKey("credit_lots.id"))
    operation_id: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(10))
    # Always positive; direction comes from the kind: credit/release add to the lot, the rest take from it
    # (a capture moves held credits out for good, so it does not change the lot).
    amount: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RunFee(Base):
    """A flat Derp fee inside a run (local media effects), added to the run's charge when it settles.

    Not provider spend, so it stays out of the provider ledger. Sponsored uses are kept at 0 credits
    to count their daily allowance.
    """

    __tablename__ = "run_fees"
    __table_args__ = (
        CheckConstraint("credits >= 0", name="credits"),
        Index("ix_run_fees_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(index=True)
    chat_id: Mapped[int]
    user_id: Mapped[int]
    item: Mapped[str] = mapped_column(String(32))
    funding: Mapped[str] = mapped_column(String(9))
    credits: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OfferNotice(Base):
    """The last UTC day a person was shown the out-of-Derpcoins offer in a group (one row per person and chat)."""

    __tablename__ = "offer_notices"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    day: Mapped[date]
