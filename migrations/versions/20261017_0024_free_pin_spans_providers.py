"""free pin spans providers

Free models are rate-limited per model and per upstream provider, and a busy
free chain failed the turn. The live free pin keeps its primary; its fallbacks
become one free model from another provider and then OpenRouter's free router
(``openrouter/free``: any free model that supports the request's tools). Two
fallbacks, so the pin still validates in the previous release. The replaced
pin stays in ``previous`` (owner Rollback in /stats).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-17 19:00:47.288499
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROUTER = "openrouter/free"
INKLING = "thinkingmachines/inkling:free"
GEMMA = "google/gemma-4-26b-a4b-it:free"


def upgrade() -> None:
    op.execute(
        sa.text(
            "update model_pins set previous = pin, source = 'owner', updated_at = now(),"
            " pin = jsonb_set(pin, '{fallbacks}', case when pin->>'model' = :gemma"
            " then cast(:after_gemma as jsonb) else cast(:after_other as jsonb) end)"
            " where role = 'chat_free' and pin->>'model' <> :router"
            " and not coalesce(pin->'fallbacks', cast('[]' as jsonb)) @> cast(:router_list as jsonb)"
        ).bindparams(
            gemma=GEMMA,
            router=ROUTER,
            after_gemma=json.dumps([INKLING, ROUTER]),
            after_other=json.dumps([GEMMA, ROUTER]),
            router_list=json.dumps([ROUTER]),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "update model_pins set pin = previous, previous = pin, source = 'owner', updated_at = now()"
            " where role = 'chat_free' and previous is not null"
            " and pin->'fallbacks' @> cast(:router_list as jsonb)"
        ).bindparams(router_list=json.dumps([ROUTER]))
    )
