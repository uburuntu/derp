"""runs and provider calls

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 00:10:59.130303
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger", sa.String(length=10), nullable=False),
        sa.Column("trigger_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("funding", sa.String(length=12), nullable=False),
        sa.Column("wallet_id", sa.BigInteger(), nullable=True),
        sa.Column("reservation_id", sa.BigInteger(), nullable=True),
        sa.Column("served_model", sa.String(length=128), nullable=True),
        sa.Column("context_tokens", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False),
        sa.Column("tool_calls", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=8), nullable=False),
        sa.Column("charged_credits", sa.BigInteger(), nullable=False),
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running', 'awaiting_approval', 'completed', 'failed', 'cancelled', 'denied', 'expired')",
            name=op.f("ck_agent_runs_status"),
        ),
        sa.CheckConstraint(
            "trigger in ('message', 'command', 'job', 'inline', 'guest', 'callback')",
            name=op.f("ck_agent_runs_trigger"),
        ),
        sa.CheckConstraint(
            "charged_credits >= 0 and cost_usd >= 0", name=op.f("ck_agent_runs_money_nonnegative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_chat_id_created_at", "agent_runs", ["chat_id", "created_at"], unique=False)
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("downstream_provider", sa.String(length=64), nullable=True),
        sa.Column("generation_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("funding", sa.String(length=12), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False),
        sa.Column("web_search_requests", sa.BigInteger(), nullable=False),
        sa.Column("units", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind in ('chat', 'stt', 'image', 'digest', 'scout', 'media')",
            name=op.f("ck_provider_calls_kind"),
        ),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_provider_calls_cost_nonnegative")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_provider_calls_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_calls")),
    )
    op.create_index("ix_provider_calls_created_at", "provider_calls", ["created_at"], unique=False)
    op.create_index(op.f("ix_provider_calls_run_id"), "provider_calls", ["run_id"], unique=False)
    op.create_table(
        "run_outputs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind in ('message', 'image', 'poll', 'sticker', 'dice', 'reaction', 'invoice', 'other')",
            name=op.f("ck_run_outputs_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"], name=op.f("fk_run_outputs_run_id_agent_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_outputs")),
    )
    op.create_index(
        "ix_run_outputs_chat_id_message_id", "run_outputs", ["chat_id", "message_id"], unique=False
    )
    op.create_index(op.f("ix_run_outputs_run_id"), "run_outputs", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_run_outputs_run_id"), table_name="run_outputs")
    op.drop_index("ix_run_outputs_chat_id_message_id", table_name="run_outputs")
    op.drop_table("run_outputs")
    op.drop_index(op.f("ix_provider_calls_run_id"), table_name="provider_calls")
    op.drop_index("ix_provider_calls_created_at", table_name="provider_calls")
    op.drop_table("provider_calls")
    op.drop_index("ix_agent_runs_chat_id_created_at", table_name="agent_runs")
    op.drop_table("agent_runs")
