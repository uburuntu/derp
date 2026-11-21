"""retain authorization on scheduled rule actions

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-21 13:44:06.753881
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("rule_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_jobs_rule_id_reply_rules"),
        "jobs",
        "reply_rules",
        ["rule_id"],
        ["id"],
        ondelete="CASCADE",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_jobs_rule_id_reply_rules"), "jobs", type_="foreignkey")
    op.drop_column("jobs", "rule_id")
