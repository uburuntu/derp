"""speech calls in the provider ledger

Voice messages Derp performs (the speech pin) are ledgered as
``kind='speech'``, with the characters spoken as ``units``. Expand-only: the
check constraint allows one more kind, which old code never writes.
Autogenerate does not compare check constraints, so this one is written out.
The new constraint is added ``NOT VALID`` and validated after that commits
(an autocommit block), so the exclusive lock is never held through the full
scan. The downgrade keeps cost history: speech rows become ``image`` rows
(media made for a run, ledgered on delivery).

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-18 16:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'chat', 'stt', 'image', 'digest', 'scout', 'media', 'decision'"
NAME = "ck_provider_calls_kind"


def _replace_check(kinds: str) -> None:
    op.drop_constraint(op.f(NAME), "provider_calls", type_="check")
    op.execute(f"alter table provider_calls add constraint {NAME} check (kind in ({kinds})) not valid")
    with op.get_context().autocommit_block():  # the scan after the swap commits: no exclusive lock held
        op.execute(f"alter table provider_calls validate constraint {NAME}")


def upgrade() -> None:
    _replace_check(f"{KINDS}, 'speech'")


def downgrade() -> None:
    op.execute("update provider_calls set kind = 'image' where kind = 'speech'")
    _replace_check(KINDS)
