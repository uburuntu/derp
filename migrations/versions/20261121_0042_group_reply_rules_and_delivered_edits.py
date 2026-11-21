"""group reply rules and delivered edits

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-21 13:35:17.680720
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# CHECK expressions need explicit changes: Alembic cannot infer widening a list of accepted values.
KINDS = (
    (
        "messages",
        "kind",
        (
            "text photo video voice video_note audio document sticker animation poll dice "
            "location venue contact checklist story game invoice giveaway paid_media service other"
        ),
        "live_photo",
    ),
    ("agent_runs", "trigger", "message command job inline guest callback", "rule"),
    ("run_outputs", "kind", "message image poll sticker dice reaction invoice other", "edit"),
    ("approvals", "kind", "image", "rule_action"),
)


def _checks(*, expand: bool) -> None:
    for table, column, previous, added in KINDS:
        values = ", ".join(repr(value) for value in (previous + (" " + added if expand else "")).split())
        name = f"ck_{table}_{column}"
        validation = " NOT VALID" if expand else ""
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT {name}, "
            f"ADD CONSTRAINT {name} CHECK ({column} IN ({values})){validation}"
        )


def upgrade() -> None:
    _checks(expand=True)
    op.create_table(
        "reply_rule_checks",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("matched", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id", "message_id"],
            ["messages.chat_id", "messages.message_id"],
            name=op.f("fk_reply_rule_checks_chat_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", "message_id", name=op.f("pk_reply_rule_checks")),
    )
    op.create_table(
        "reply_rules",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("whole_chat", sa.Boolean(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name", sa.String(), nullable=True),
        sa.Column("condition", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("needs_media", sa.Boolean(), nullable=False),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("replaces_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("state in ('draft', 'active', 'paused')", name=op.f("ck_reply_rules_state")),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.id"], name=op.f("fk_reply_rules_chat_id_chats"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["replaces_id"],
            ["reply_rules.id"],
            name=op.f("fk_reply_rules_replaces_id_reply_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reply_rules")),
    )
    op.create_index("ix_reply_rules_chat_state", "reply_rules", ["chat_id", "state"], unique=False)
    op.add_column("provider_calls", sa.Column("reservation_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_provider_calls_reservation_id_reservations"),
        "provider_calls",
        "reservations",
        ["reservation_id"],
        ["id"],
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    _checks(expand=False)
    op.drop_constraint(
        op.f("fk_provider_calls_reservation_id_reservations"), "provider_calls", type_="foreignkey"
    )
    op.drop_column("provider_calls", "reservation_id")
    op.drop_index("ix_reply_rules_chat_state", table_name="reply_rules")
    op.drop_table("reply_rules")
    op.drop_table("reply_rule_checks")
