"""notes and chat rules

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16 00:51:32.576854
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state in ('active', 'proposed')", name=op.f("ck_notes_state")),
        sa.CheckConstraint("char_length(text) between 1 and 500", name=op.f("ck_notes_text_length")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    op.create_index("ix_notes_chat_user", "notes", ["chat_id", "user_id"], unique=False)
    op.add_column("chats", sa.Column("rules", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "rules")
    op.drop_index("ix_notes_chat_user", table_name="notes")
    op.drop_table("notes")
