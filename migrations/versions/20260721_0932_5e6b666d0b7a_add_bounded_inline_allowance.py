"""add bounded inline allowance

Revision ID: 5e6b666d0b7a
Revises: 7e1fee80789f
Create Date: 2026-07-21 09:32:31.528930+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e6b666d0b7a"
down_revision: str | None = "7e1fee80789f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inline_daily_allowances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "used_count >= 0", name="inline_daily_allowance_used_non_negative"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "usage_date", name="uq_inline_daily_allowance_user_date"
        ),
    )
    op.create_index(
        "ix_inline_daily_allowances_usage_date",
        "inline_daily_allowances",
        ["usage_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inline_daily_allowances_usage_date", table_name="inline_daily_allowances"
    )
    op.drop_table("inline_daily_allowances")
