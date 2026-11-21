"""distinguish changes to a configured key cap

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-21 19:43:18.884051
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE key_requests DROP CONSTRAINT ck_key_requests_step,
        ADD CONSTRAINT ck_key_requests_step CHECK (step IN ('paste', 'cap', 'model', 'adjust'))
    """)


def downgrade() -> None:
    # Preserve an owner's pending typed amount as a cap request the previous release understands.
    op.execute("LOCK TABLE key_requests IN ACCESS EXCLUSIVE MODE")
    op.execute("UPDATE key_requests SET step = 'cap' WHERE step = 'adjust'")
    op.execute("""
        ALTER TABLE key_requests DROP CONSTRAINT ck_key_requests_step,
        ADD CONSTRAINT ck_key_requests_step CHECK (step IN ('paste', 'cap', 'model'))
    """)
