"""contributed keys

People's own OpenAI/Anthropic API keys powering the chats they plug them
into: ``contributed_keys`` (registered at OpenRouter behind a sealed
sub-key), ``key_chats`` (where each serves), ``key_requests`` (a plug-in in
progress) and ``key_models`` (the scout's look at the curated models). Runs
and provider calls a key paid name it (``key_id``), with what its provider
charged (``provider_calls.upstream_usd``). Expand-only: old code never reads
the new tables or columns.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-04 13:12:33.598723
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contributed_keys",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=10), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("cap_usd", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("cycle_anchor", sa.Date(), nullable=False),
        sa.Column("sub_key_hash", sa.String(length=64), nullable=False),
        sa.Column("sub_key", sa.LargeBinary(), nullable=False),
        sa.Column("credential_id", sa.String(length=64), nullable=False),
        sa.Column("paused", sa.String(length=10), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("noticed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "paused is null or paused in ('cap', 'exhausted', 'owner')",
            name=op.f("ck_contributed_keys_paused"),
        ),
        sa.CheckConstraint("provider in ('openai', 'anthropic')", name=op.f("ck_contributed_keys_provider")),
        sa.CheckConstraint("cap_usd > 0", name=op.f("ck_contributed_keys_cap_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contributed_keys")),
        sa.UniqueConstraint("sub_key_hash", name=op.f("uq_contributed_keys_sub_key_hash")),
    )
    op.create_index("ix_contributed_keys_user_id", "contributed_keys", ["user_id"], unique=False)
    op.create_table(
        "key_models",
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("live", sa.Boolean(), nullable=False),
        sa.Column("price_prompt", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("price_completion", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("price_cache_read", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("smoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("model", name=op.f("pk_key_models")),
    )
    op.create_table(
        "key_chats",
        sa.Column("key_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("enabled_by", sa.BigInteger(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_by", sa.BigInteger(), nullable=True),
        sa.Column("card_message_id", sa.BigInteger(), nullable=True),
        sa.Column("refusal_noticed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_noticed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("off_noticed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state in ('offered', 'on', 'paused', 'off')", name=op.f("ck_key_chats_state")),
        sa.ForeignKeyConstraint(
            ["key_id"], ["contributed_keys.id"], name=op.f("fk_key_chats_key_id_contributed_keys")
        ),
        sa.PrimaryKeyConstraint("key_id", "chat_id", name=op.f("pk_key_chats")),
    )
    op.create_index("ix_key_chats_chat_id_state", "key_chats", ["chat_id", "state"], unique=False)
    op.create_table(
        "key_requests",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("step", sa.String(length=6), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("key_id", sa.BigInteger(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("step in ('paste', 'cap', 'model')", name=op.f("ck_key_requests_step")),
        sa.ForeignKeyConstraint(
            ["key_id"], ["contributed_keys.id"], name=op.f("fk_key_requests_key_id_contributed_keys")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_key_requests")),
    )
    op.add_column("agent_runs", sa.Column("key_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_agent_runs_key_id_contributed_keys"), "agent_runs", "contributed_keys", ["key_id"], ["id"]
    )
    op.add_column("provider_calls", sa.Column("key_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "provider_calls", sa.Column("upstream_usd", sa.Numeric(precision=14, scale=8), nullable=True)
    )
    op.create_index(op.f("ix_provider_calls_key_id"), "provider_calls", ["key_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_provider_calls_key_id_contributed_keys"),
        "provider_calls",
        "contributed_keys",
        ["key_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_provider_calls_key_id_contributed_keys"), "provider_calls", type_="foreignkey"
    )
    op.drop_index(op.f("ix_provider_calls_key_id"), table_name="provider_calls")
    op.drop_column("provider_calls", "upstream_usd")
    op.drop_column("provider_calls", "key_id")
    op.drop_constraint(op.f("fk_agent_runs_key_id_contributed_keys"), "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "key_id")
    op.drop_table("key_requests")
    op.drop_index("ix_key_chats_chat_id_state", table_name="key_chats")
    op.drop_table("key_chats")
    op.drop_table("key_models")
    op.drop_index("ix_contributed_keys_user_id", table_name="contributed_keys")
    op.drop_table("contributed_keys")
