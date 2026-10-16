"""digests

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16 08:52:01.584938
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("thread_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("first_message_id", sa.BigInteger(), nullable=False),
        sa.Column("last_message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_count", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "thread_id", "day", name=op.f("pk_digests")),
    )


def downgrade() -> None:
    op.drop_table("digests")
