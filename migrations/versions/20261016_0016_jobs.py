"""jobs

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-16 09:19:48.016743
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("repeat", sa.String(length=8), nullable=True),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("misses", sa.BigInteger(), nullable=False),
        sa.Column("runs", sa.BigInteger(), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind in ('message', 'prompt')", name=op.f("ck_jobs_kind")),
        sa.CheckConstraint(
            "repeat is null or repeat in ('daily', 'weekdays', 'weekly', 'monthly')",
            name=op.f("ck_jobs_repeat"),
        ),
        sa.CheckConstraint("state in ('active', 'paused', 'done', 'cancelled')", name=op.f("ck_jobs_state")),
        sa.CheckConstraint("char_length(text) between 1 and 1000", name=op.f("ck_jobs_text_length")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_chat_id_state", "jobs", ["chat_id", "state"], unique=False)
    op.create_index("ix_jobs_state_next_at", "jobs", ["state", "next_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_state_next_at", table_name="jobs")
    op.drop_index("ix_jobs_chat_id_state", table_name="jobs")
    op.drop_table("jobs")
