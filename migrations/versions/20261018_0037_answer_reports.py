"""answer reports

/info's Report a problem files feedback about one answer: three new kinds
(wrong_answer, missed_context, wrong_image; "bug" is the fourth category),
so the kind column widens, and ``details_until`` marks the short window in
which the author's next message becomes the report's text. Expand-only: old
code writes the old kinds and never reads the new column.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-18 13:08:42.642364
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "kind in ('bug', 'idea', 'praise', 'other')"
NEW_KINDS = "kind in ('bug', 'idea', 'praise', 'other', 'wrong_answer', 'missed_context', 'wrong_image')"


def upgrade() -> None:
    op.add_column("feedback", sa.Column("details_until", sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        "feedback",
        "kind",
        existing_type=sa.VARCHAR(length=8),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.drop_constraint(op.f("ck_feedback_kind"), "feedback", type_="check")
    op.create_check_constraint(op.f("ck_feedback_kind"), "feedback", NEW_KINDS)
    op.create_index(
        "ix_feedback_details",
        "feedback",
        ["user_id"],
        unique=False,
        postgresql_where="details_until is not null",
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_details", table_name="feedback", postgresql_where="details_until is not null")
    op.execute(
        "update feedback set kind = 'bug' where kind in ('wrong_answer', 'missed_context', 'wrong_image')"
    )
    op.drop_constraint(op.f("ck_feedback_kind"), "feedback", type_="check")
    op.create_check_constraint(op.f("ck_feedback_kind"), "feedback", OLD_KINDS)
    op.alter_column(
        "feedback",
        "kind",
        existing_type=sa.String(length=16),
        type_=sa.VARCHAR(length=8),
        existing_nullable=False,
    )
    op.drop_column("feedback", "details_until")
