"""switch chat pins to deepseek 1017

Live pins are DB rows that the seed never overwrites, so a change of default
chat model reaches production as data. DeepSeek V4.1 Flash 1017 (a dated
snapshot) becomes the primary for paid and sponsored chat with GLM 5.3 Flash as
its fallback: same list price, images and tools work over ZDR routing, and its
cache reads cost a tenth of GLM's. The replaced pin stays in ``previous``
(owner Rollback in /stats).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-17 18:55:53.486243
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"


def pin(*, fast: bool) -> str:
    return json.dumps(
        {
            "model": DEEPSEEK,
            "fallbacks": ["z-ai/glm-5.3-flash"],
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
        " price_prompt = 0.15, price_completion = 0.6, updated_at = now()"
        " where role = :role and pin->>'model' <> :model"
    )
    op.execute(switch.bindparams(role="chat", pin=pin(fast=True), model=DEEPSEEK))
    op.execute(switch.bindparams(role="chat_sponsored", pin=pin(fast=False), model=DEEPSEEK))


def downgrade() -> None:
    op.execute(
        sa.text(
            "update model_pins set pin = previous, previous = pin, source = 'owner', updated_at = now()"
            " where role in ('chat', 'chat_sponsored') and pin->>'model' = :model and previous is not null"
        ).bindparams(model=DEEPSEEK)
    )
