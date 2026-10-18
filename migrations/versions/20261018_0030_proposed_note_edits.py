"""proposed note edits

A non-admin's edit of a chat note in admins mode is kept as a proposed note
pointing at the note it rewrites once approved (gone with that note). A
nullable column only; old code shows such an edit as a plain proposal.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-18 11:56:23.801097
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("replaces_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_notes_replaces_id", "notes", ["replaces_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_notes_replaces_id_notes"), "notes", "notes", ["replaces_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_notes_replaces_id_notes"), "notes", type_="foreignkey")
    op.drop_index("ix_notes_replaces_id", table_name="notes")
    op.drop_column("notes", "replaces_id")
