"""wallet ledger

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16 08:11:41.464127
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind in ('user', 'chat')", name=op.f("ck_wallets_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallets")),
        sa.UniqueConstraint("kind", "owner_id", name=op.f("uq_wallets_kind_owner_id")),
    )
    op.create_table(
        "credit_lots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("wallet_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("remaining", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "source in ('plan', 'purchase', 'sponsored', 'grant', 'migration')",
            name=op.f("ck_credit_lots_source"),
        ),
        sa.CheckConstraint(
            "amount > 0 and remaining >= 0 and remaining <= amount",
            name=op.f("ck_credit_lots_remaining_in_range"),
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], name=op.f("fk_credit_lots_wallet_id_wallets")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_lots")),
        sa.UniqueConstraint("operation_id", name=op.f("uq_credit_lots_operation_id")),
    )
    op.create_index(
        "ix_credit_lots_wallet_id_expires_at", "credit_lots", ["wallet_id", "expires_at"], unique=False
    )
    op.create_table(
        "reservations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("wallet_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("captured", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state in ('held', 'captured', 'released')", name=op.f("ck_reservations_state")),
        sa.CheckConstraint(
            "amount > 0 and captured >= 0 and captured <= amount",
            name=op.f("ck_reservations_captured_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallets.id"], name=op.f("fk_reservations_wallet_id_wallets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservations")),
        sa.UniqueConstraint("operation_id", name=op.f("uq_reservations_operation_id")),
    )
    op.create_index("ix_reservations_state_created_at", "reservations", ["state", "created_at"], unique=False)
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("wallet_id", sa.BigInteger(), nullable=False),
        sa.Column("lot_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind in ('credit', 'hold', 'capture', 'release', 'expire', 'clawback')",
            name=op.f("ck_ledger_entries_kind"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_ledger_entries_amount_positive")),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["credit_lots.id"], name=op.f("fk_ledger_entries_lot_id_credit_lots")
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallets.id"], name=op.f("fk_ledger_entries_wallet_id_wallets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
        sa.UniqueConstraint(
            "operation_id", "kind", "lot_id", name=op.f("uq_ledger_entries_operation_id_kind_lot_id")
        ),
    )
    op.create_index(
        "ix_ledger_entries_wallet_id_created_at", "ledger_entries", ["wallet_id", "created_at"], unique=False
    )
    op.create_table(
        "reservation_holds",
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("lot_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_reservation_holds_amount_positive")),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["credit_lots.id"], name=op.f("fk_reservation_holds_lot_id_credit_lots")
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name=op.f("fk_reservation_holds_reservation_id_reservations"),
        ),
        sa.PrimaryKeyConstraint("reservation_id", "lot_id", name=op.f("pk_reservation_holds")),
    )
    # The ledger is append-only: reject updates and deletes at the database level.
    op.execute(
        "create function ledger_entries_append_only() returns trigger language plpgsql as "
        "$$ begin raise exception 'ledger_entries is append-only'; end $$"
    )
    op.execute(
        "create trigger ledger_entries_append_only before update or delete on ledger_entries "
        "for each row execute function ledger_entries_append_only()"
    )


def downgrade() -> None:
    op.execute("drop trigger ledger_entries_append_only on ledger_entries")
    op.execute("drop function ledger_entries_append_only()")
    op.drop_table("reservation_holds")
    op.drop_index("ix_ledger_entries_wallet_id_created_at", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("ix_reservations_state_created_at", table_name="reservations")
    op.drop_table("reservations")
    op.drop_index("ix_credit_lots_wallet_id_expires_at", table_name="credit_lots")
    op.drop_table("credit_lots")
    op.drop_table("wallets")
