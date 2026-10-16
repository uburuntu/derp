"""sticker index

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16 00:58:18.765253
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stickers",
        sa.Column("file_unique_id", sa.String(length=64), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("emoji", sa.String(length=16), nullable=True),
        sa.Column("set_name", sa.String(), nullable=True),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("curated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("uses", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("file_unique_id", name=op.f("pk_stickers")),
    )
    op.create_index("ix_stickers_emoji", "stickers", ["emoji"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stickers_emoji", table_name="stickers")
    op.drop_table("stickers")
