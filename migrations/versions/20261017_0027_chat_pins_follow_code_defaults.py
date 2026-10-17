"""chat pins follow code defaults

At startup the pin seed now updates rows still marked ``seed`` when the code
default changes, so switching the default chat model needs no data migration.
The chat roles were last set by migrations as ``owner``; they match today's
defaults, so they go back to ``seed`` and follow the code from now on. Pins
the scout healed keep ``scout``.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-17 19:41:12.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "update model_pins set source = 'seed', updated_at = now()"
        " where role in ('chat', 'chat_sponsored', 'chat_free') and source = 'owner'"
    )


def downgrade() -> None:
    op.execute(
        "update model_pins set source = 'owner', updated_at = now()"
        " where role in ('chat', 'chat_sponsored', 'chat_free') and source = 'seed'"
    )
