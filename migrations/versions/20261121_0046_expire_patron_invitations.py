"""expire patron invitations

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-21 19:45:03.205855
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("patron_posts", sa.Column("chip_until", sa.DateTime(timezone=True)), if_not_exists=True)
    op.alter_column("patron_posts", "chip_until", server_default=sa.text("now() + interval '30 minutes'"))
    # Release the DDL lock before the scan. Older invitations retire on their first tap rather
    # than scheduling edits of historical group messages immediately after deploy.
    with op.get_context().autocommit_block():
        op.execute("""UPDATE patron_posts SET chip_until = created_at + interval '30 minutes'
            WHERE chip_until IS NULL AND created_at > now() - interval '30 minutes'""")
        op.create_index(
            "ix_patron_posts_chip_until",
            "patron_posts",
            ["chip_until"],
            if_not_exists=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    op.drop_index("ix_patron_posts_chip_until", table_name="patron_posts")
    op.drop_column("patron_posts", "chip_until")
