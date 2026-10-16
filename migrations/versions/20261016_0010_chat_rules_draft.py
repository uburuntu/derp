"""chat rules draft

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16 08:26:03.614752
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("rules_draft", sa.String(), nullable=True))
    op.add_column("chats", sa.Column("rules_draft_by", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "rules_draft_by")
    op.drop_column("chats", "rules_draft")
