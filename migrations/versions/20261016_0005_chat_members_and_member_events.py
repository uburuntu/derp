"""chat members and member events

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16 00:22:12.507434
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_members",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("status", sa.String(length=13), nullable=False),
        sa.Column("tag", sa.String(), nullable=True),
        sa.Column(
            "names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("message_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('creator', 'administrator', 'member', 'restricted', 'left', 'kicked')",
            name=op.f("ck_chat_members_status"),
        ),
        sa.PrimaryKeyConstraint("chat_id", "user_id", name=op.f("pk_chat_members")),
    )
    op.create_table(
        "member_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind in ('join', 'leave', 'promote', 'demote', 'ban', 'unban', 'restrict', 'title')",
            name=op.f("ck_member_events_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_events")),
    )
    op.create_index("ix_member_events_chat_at", "member_events", ["chat_id", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_member_events_chat_at", table_name="member_events")
    op.drop_table("member_events")
    op.drop_table("chat_members")
