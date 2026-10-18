"""signed quiz points

A day's quiz points are now the plain sum of +1/−1 answers, floored at zero
only where totals are read, so the order of answers no longer matters; the
row itself may go negative. Expand-only: old code writes non-negative points,
which the looser column still takes.

Revision ID: 0035
Revises: 0029
Create Date: 2026-10-18 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_game_scores_points"), "game_scores", type_="check")


def downgrade() -> None:
    op.execute("update game_scores set points = 0 where points < 0")
    op.create_check_constraint(op.f("ck_game_scores_points"), "game_scores", "points >= 0")
