"""switch chat pins to glm

Live pins are DB rows that the seed never overwrites, so a change of default
chat model reaches production as data. The replaced pin stays in ``previous``
(owner Rollback in /stats). Also drops the retired frontier role and dismisses
open scout proposals for OpenAI/Anthropic chat models, which the scout no
longer considers.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-16 20:35:10.144781
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GLM = "z-ai/glm-5.3-flash"


def pin(*, fast: bool) -> str:
    return json.dumps(
        {
            "model": GLM,
            "fallbacks": ["deepseek/deepseek-v4.1-flash"],
            "zdr": True,
            "reasoning": "low",
            "vision": True,
            "fast": fast,
            "max_price_prompt": None,
            "max_price_completion": None,
            "only": {},
        }
    )


def upgrade() -> None:
    switch = sa.text(
        "update model_pins set previous = pin, pin = cast(:pin as jsonb), source = 'owner',"
        " price_prompt = 0.15, price_completion = 0.5, updated_at = now()"
        " where role = :role and pin->>'model' <> :model"
    )
    op.execute(switch.bindparams(role="chat", pin=pin(fast=True), model=GLM))
    op.execute(switch.bindparams(role="chat_sponsored", pin=pin(fast=False), model=GLM))
    op.execute("delete from model_pins where role = 'chat_frontier'")
    op.execute(
        "update scout_proposals set status = 'ignored', decided_at = now() where status = 'open'"
        " and (proposed->>'model' like 'openai/%' or proposed->>'model' like 'anthropic/%')"
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "update model_pins set pin = previous, previous = pin, source = 'owner', updated_at = now()"
            " where role in ('chat', 'chat_sponsored') and pin->>'model' = :model and previous is not null"
        ).bindparams(model=GLM)
    )
