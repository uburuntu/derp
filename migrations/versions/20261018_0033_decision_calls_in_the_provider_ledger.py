"""decision calls in the provider ledger

Calls to the decision pin (typed questions before a run) are ledgered as
``kind='decision'``. Expand-only: the check constraint allows one more kind,
which old code never writes. Autogenerate does not compare check constraints,
so this one is written out.

Revision ID: 0033
Revises: 0031
Create Date: 2026-10-18 12:02:43.381403
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'chat', 'stt', 'image', 'digest', 'scout', 'media'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_provider_calls_kind"), "provider_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_provider_calls_kind"), "provider_calls", f"kind in ({KINDS}, 'decision')"
    )


def downgrade() -> None:
    op.execute("delete from provider_calls where kind = 'decision'")
    op.drop_constraint(op.f("ck_provider_calls_kind"), "provider_calls", type_="check")
    op.create_check_constraint(op.f("ck_provider_calls_kind"), "provider_calls", f"kind in ({KINDS})")
