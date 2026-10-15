"""reactions and polls

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16 00:27:07.979985
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "poll_votes",
        sa.Column("poll_id", sa.String(length=64), nullable=False),
        sa.Column("voter_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("option_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("option_persistent_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("voted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("poll_id", "voter_id", name=op.f("pk_poll_votes")),
    )
    op.create_table(
        "polls",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total_voters", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("type", sa.String(length=8), nullable=False),
        sa.Column("correct_option_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("explanation", sa.String(), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False),
        sa.Column("multiple", sa.Boolean(), nullable=False),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("close_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin", sa.String(length=9), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("origin in ('derp_poll', 'derp_quiz', 'user')", name=op.f("ck_polls_origin")),
        sa.CheckConstraint("type in ('regular', 'quiz')", name=op.f("ck_polls_type")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_polls")),
    )
    op.create_index("ix_polls_chat_message", "polls", ["chat_id", "message_id"], unique=False)
    op.create_table(
        "reaction_counts",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("reaction", sa.String(length=64), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "message_id", "reaction", name=op.f("pk_reaction_counts")),
    )
    op.create_table(
        "reactions",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("reaction", sa.String(length=64), nullable=False),
        sa.Column("actor_is_chat", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "message_id", "actor_id", "reaction", name=op.f("pk_reactions")),
    )


def downgrade() -> None:
    op.drop_table("reactions")
    op.drop_table("reaction_counts")
    op.drop_index("ix_polls_chat_message", table_name="polls")
    op.drop_table("polls")
    op.drop_table("poll_votes")
