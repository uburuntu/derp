"""validate reply rule constraints and build indexes

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-21 13:55:21.282974
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Schema changes and their Alembic stamps were committed together in 0042 and 0043. Only
# repeatable operations run outside the migration transaction here: each can be interrupted.
CONSTRAINTS = (
    ("messages", "ck_messages_kind"),
    ("agent_runs", "ck_agent_runs_trigger"),
    ("run_outputs", "ck_run_outputs_kind"),
    ("approvals", "ck_approvals_kind"),
    ("provider_calls", "fk_provider_calls_reservation_id_reservations"),
    ("jobs", "fk_jobs_rule_id_reply_rules"),
)
INDEXES = (
    ("provider_calls", "reservation_id", "ix_provider_calls_reservation_id"),
    ("jobs", "rule_id", "ix_jobs_rule_id"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for table, constraint in CONSTRAINTS:
            # Already validated constraints do not need another scan on a retry.
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
        for table, column, name in INDEXES:
            valid = op.get_bind().scalar(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = :name"
                ),
                {"name": name},
            )
            if valid is True:
                continue
            if valid is False:
                # A cancelled concurrent build may leave its name occupied by an invalid index.
                op.drop_index(op.f(name), table_name=table, postgresql_concurrently=True)
            op.create_index(op.f(name), table, [column], postgresql_concurrently=True)


def downgrade() -> None:
    # Index removals and the revision stamp commit together. The still-installed constraints
    # remain validated; their owning revisions remove or restore them on further downgrade.
    for table, _column, name in reversed(INDEXES):
        op.drop_index(op.f(name), table_name=table)
