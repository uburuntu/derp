"""messages media topics

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16 00:19:06.821221
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forum_topics",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("thread_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("icon_custom_emoji_id", sa.String(), nullable=True),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "thread_id", name=op.f("pk_forum_topics")),
    )
    op.create_table(
        "messages",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("text", sa.String(), nullable=True),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("quote", sa.String(), nullable=True),
        sa.Column("forward_origin", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("media_group_id", sa.String(), nullable=True),
        sa.Column("via_bot_id", sa.BigInteger(), nullable=True),
        sa.Column("capture", sa.String(length=10), nullable=False),
        sa.Column("is_derp", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extra_text", sa.String(), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(text, '') || ' ' || coalesce(extra_text, ''))", persisted=True
            ),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("capture in ('ambient', 'invocation', 'derp')", name=op.f("ck_messages_capture")),
        sa.CheckConstraint(
            "kind in ('text', 'photo', 'video', 'voice', 'video_note', 'audio', 'document', 'sticker', 'animation', 'poll', 'dice', 'location', 'venue', 'contact', 'checklist', 'story', 'game', 'invoice', 'giveaway', 'paid_media', 'service', 'other')",
            name=op.f("ck_messages_kind"),
        ),
        sa.CheckConstraint("message_id > 0", name=op.f("ck_messages_message_id_positive")),
        sa.PrimaryKeyConstraint("chat_id", "message_id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_chat_thread_date", "messages", ["chat_id", "thread_id", "date"], unique=False
    )
    op.create_index("ix_messages_chat_user_date", "messages", ["chat_id", "user_id", "date"], unique=False)
    op.create_index("ix_messages_date", "messages", ["date"], unique=False)
    op.create_index(
        "ix_messages_text_trgm",
        "messages",
        ["text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )
    op.create_index("ix_messages_tsv", "messages", ["tsv"], unique=False, postgresql_using="gin")
    op.create_table(
        "message_media",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("idx", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", sa.String(length=12), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("file_unique_id", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.BigInteger(), nullable=True),
        sa.Column("height", sa.BigInteger(), nullable=True),
        sa.Column("duration", sa.BigInteger(), nullable=True),
        sa.Column("file_name", sa.String(), nullable=True),
        sa.Column("sticker_emoji", sa.String(), nullable=True),
        sa.Column("sticker_set", sa.String(), nullable=True),
        sa.Column("transcript", sa.String(), nullable=True),
        sa.Column("transcript_model", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("description_run_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["chat_id", "message_id"],
            ["messages.chat_id", "messages.message_id"],
            name=op.f("fk_message_media_chat_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", "message_id", "idx", name=op.f("pk_message_media")),
    )


def downgrade() -> None:
    op.drop_table("message_media")
    op.drop_index("ix_messages_tsv", table_name="messages", postgresql_using="gin")
    op.drop_index(
        "ix_messages_text_trgm",
        table_name="messages",
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )
    op.drop_index("ix_messages_date", table_name="messages")
    op.drop_index("ix_messages_chat_user_date", table_name="messages")
    op.drop_index("ix_messages_chat_thread_date", table_name="messages")
    op.drop_table("messages")
    op.drop_table("forum_topics")
