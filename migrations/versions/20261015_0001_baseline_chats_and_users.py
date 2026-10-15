"""baseline chats and users

Revision ID: 0001
Revises:
Create Date: 2026-10-15 23:57:36.592790
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("create extension if not exists pg_trgm")
    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("is_forum", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("ambient", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("retention_days", sa.BigInteger(), nullable=True),
        sa.Column("notes_mode", sa.String(length=8), server_default="auto", nullable=False),
        sa.Column("free_models", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("bot_status", sa.String(length=16), nullable=True),
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "notes_mode in ('auto', 'members', 'admins', 'off')", name=op.f("ck_chats_notes_mode")
        ),
        sa.CheckConstraint(
            "type in ('private', 'group', 'supergroup', 'channel')", name=op.f("ck_chats_type")
        ),
        sa.CheckConstraint(
            "retention_days is null or retention_days > 0", name=op.f("ck_chats_retention_days")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("is_bot", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("first_name", sa.String(), server_default="", nullable=False),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("language_code", sa.String(length=16), nullable=True),
        sa.Column("is_premium", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("free_models_consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("free_models_consent_version", sa.String(length=16), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )


def downgrade() -> None:
    op.drop_table("users")
    op.drop_table("chats")
