"""subscriptions

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16 08:35:32.629985
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("first_charge_id", sa.String(length=128), nullable=False),
        sa.Column("last_charge_id", sa.String(length=128), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state in ('active', 'canceled', 'failed')", name=op.f("ck_subscriptions_state")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint("payload", name=op.f("uq_subscriptions_payload")),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
