"""decision calls in the provider ledger

Calls to the decision pin (typed questions before a run) are ledgered as
``kind='decision'``. Expand-only: the check constraint allows one more kind,
which old code never writes. Autogenerate does not compare check
constraints, so this one is written out. The new constraint is added ``NOT
VALID`` and validated after that commits (an autocommit block), so the
exclusive lock is never held through the full scan. The downgrade keeps cost
history: decision rows become ``scout`` rows (creator-funded side calls).

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-18 12:02:43.381403
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'chat', 'stt', 'image', 'digest', 'scout', 'media'"
NAME = "ck_provider_calls_kind"


def _replace_check(kinds: str) -> None:
    op.drop_constraint(op.f(NAME), "provider_calls", type_="check")
    op.execute(f"alter table provider_calls add constraint {NAME} check (kind in ({kinds})) not valid")
    with op.get_context().autocommit_block():  # the scan after the swap commits: no exclusive lock held
        op.execute(f"alter table provider_calls validate constraint {NAME}")


def upgrade() -> None:
    _replace_check(f"{KINDS}, 'decision'")


def downgrade() -> None:
    op.execute("update provider_calls set kind = 'scout' where kind = 'decision'")
    _replace_check(KINDS)
