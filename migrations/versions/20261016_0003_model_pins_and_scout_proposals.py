"""model pins and scout proposals

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 00:14:28.439752
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_pins",
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("pin", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("previous", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("price_prompt", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("price_completion", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("role", name=op.f("pk_model_pins")),
    )
    op.create_table(
        "scout_proposals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("current", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("proposed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('open', 'applied', 'ignored')", name=op.f("ck_scout_proposals_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scout_proposals")),
    )


def downgrade() -> None:
    op.drop_table("scout_proposals")
    op.drop_table("model_pins")
