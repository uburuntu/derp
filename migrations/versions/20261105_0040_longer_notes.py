"""longer notes

A note may hold up to 1000 characters (was 500), so a list kept as one note
has room to grow. Expand-only: old code writes notes of up to 500, which the
wider check still takes.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-05 14:42:34.260167
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_notes_text_length"), "notes", type_="check")
    op.create_check_constraint(op.f("ck_notes_text_length"), "notes", "char_length(text) between 1 and 1000")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_notes_text_length"), "notes", type_="check")
    op.execute("update notes set text = left(text, 500) where char_length(text) > 500")
    op.create_check_constraint(op.f("ck_notes_text_length"), "notes", "char_length(text) between 1 and 500")
