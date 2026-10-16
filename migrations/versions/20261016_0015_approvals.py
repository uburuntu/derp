"""approvals

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16 09:13:44.901092
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_to", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("credits", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind in ('image')", name=op.f("ck_approvals_kind")),
        sa.CheckConstraint(
            "state in ('pending', 'approved', 'denied', 'expired')", name=op.f("ck_approvals_state")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )


def downgrade() -> None:
    op.drop_table("approvals")
