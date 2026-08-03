"""retire inline allowance and track refunds

Revision ID: dcca6f548b5b
Revises: 98772896c1f4
Create Date: 2026-07-28 12:55:11.545775+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dcca6f548b5b"
down_revision: str | None = "98772896c1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSOLETE_FREE_TABLES = (
    "free_inference_admissions",
    "free_inference_daily_users",
    "free_inference_daily_global",
)


def upgrade() -> None:
    # Older release-candidate schemas briefly contained these unshipped tables.
    # Conditional cleanup keeps upgrades compatible with both observed shapes.
    for table_name in _OBSOLETE_FREE_TABLES:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table_name}"'))

    # Some databases were stamped at 987 while its short-lived direct-parent
    # graph was deployed and therefore never created this table.
    op.execute(sa.text("DROP TABLE IF EXISTS inline_daily_allowances"))
    op.drop_constraint(
        "payment_receipt_status_allowed",
        "payment_receipts",
        type_="check",
    )
    op.create_check_constraint(
        "payment_receipt_status_allowed",
        "payment_receipts",
        "status = ANY (ARRAY["
        "'received', 'fulfilled', 'refund_requested', 'clawed_back', "
        "'needs_review'])",
    )


def downgrade() -> None:
    op.drop_constraint(
        "payment_receipt_status_allowed",
        "payment_receipts",
        type_="check",
    )
    op.create_check_constraint(
        "payment_receipt_status_allowed",
        "payment_receipts",
        "status = ANY (ARRAY['received', 'fulfilled', 'clawed_back', 'needs_review'])",
    )
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
            "used_count >= 0",
            name="inline_daily_allowance_used_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "usage_date",
            name="uq_inline_daily_allowance_user_date",
        ),
    )
    op.create_index(
        "ix_inline_daily_allowances_usage_date",
        "inline_daily_allowances",
        ["usage_date"],
        unique=False,
    )
