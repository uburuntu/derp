"""waiting questions

An ask nobody could pay for is remembered per person and chat
(``chat_members.waiting_message_id`` / ``waiting_at``) so a purchase or the
free-models opt-in can resume it once. Expand-only: two nullable columns old
code ignores.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-18 12:13:53.242649
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_members", sa.Column("waiting_message_id", sa.BigInteger(), nullable=True))
    op.add_column("chat_members", sa.Column("waiting_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_members", "waiting_at")
    op.drop_column("chat_members", "waiting_message_id")
