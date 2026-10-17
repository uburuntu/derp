"""daily titles

"<title> of the day" winners: one row per chat, normalized title and chat-local
day, so asking twice gives the same winner and counts make a leaderboard. A new
table only; old code ignores it.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-17 18:49:14.461226
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_titles",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "title", "day", name=op.f("pk_daily_titles")),
    )
    op.create_index("ix_daily_titles_chat_user", "daily_titles", ["chat_id", "user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_daily_titles_chat_user", table_name="daily_titles")
    op.drop_table("daily_titles")
