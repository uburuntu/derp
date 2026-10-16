"""purchases

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 08:31:51.113851
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "purchases",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_charge_id", sa.String(length=128), nullable=False),
        sa.Column("provider_charge_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("wallet_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=4), nullable=False),
        sa.Column("product", sa.String(length=32), nullable=False),
        sa.Column("stars", sa.BigInteger(), nullable=False),
        sa.Column("credits", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.String(length=128), nullable=False),
        sa.Column("is_recurring", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('pack', 'plan')", name=op.f("ck_purchases_kind")),
        sa.CheckConstraint("status in ('paid', 'refunded')", name=op.f("ck_purchases_status")),
        sa.CheckConstraint("stars > 0 and credits > 0", name=op.f("ck_purchases_amounts_positive")),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], name=op.f("fk_purchases_wallet_id_wallets")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchases")),
        sa.UniqueConstraint("telegram_charge_id", name=op.f("uq_purchases_telegram_charge_id")),
    )


def downgrade() -> None:
    op.drop_table("purchases")
