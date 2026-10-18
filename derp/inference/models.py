"""The provider-call ledger: one row per paid provider call, whoever funded it."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

COST = Numeric(14, 8)


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    __table_args__ = (
        CheckConstraint(
            "kind in ('chat', 'stt', 'image', 'digest', 'scout', 'media', 'decision', 'speech')", name="kind"
        ),
        CheckConstraint("cost_usd >= 0", name="cost_nonnegative"),
        Index("ix_provider_calls_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    role: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128))  # the model that actually served the call
    downstream_provider: Mapped[str | None] = mapped_column(String(64))
    generation_id: Mapped[str | None] = mapped_column(String(128))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True)
    chat_id: Mapped[int | None]
    user_id: Mapped[int | None]
    funding: Mapped[str] = mapped_column(String(12))  # paid | sponsored | free | creator
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    reasoning_tokens: Mapped[int] = mapped_column(default=0)
    web_search_requests: Mapped[int] = mapped_column(default=0)
    units: Mapped[int] = mapped_column(default=0)  # images generated / audio seconds / characters spoken
    cost_usd: Mapped[Decimal] = mapped_column(COST, default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModelPin(Base):
    """The live pin for a hidden model role; seeded from settings, maintained by the scout."""

    __tablename__ = "model_pins"

    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    pin: Mapped[dict[str, Any]]  # RolePin JSON
    previous: Mapped[dict[str, Any] | None]  # rollback target
    price_prompt: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6)
    )  # USD per 1M tokens, cheapest ZDR route
    price_completion: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    source: Mapped[str] = mapped_column(String(8), default="seed")  # seed | scout | owner
    checked_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ScoutProposal(Base):
    """A pin change that needs the owner's Apply (anything beyond safe auto-fixes)."""

    __tablename__ = "scout_proposals"
    __table_args__ = (CheckConstraint("status in ('open', 'applied', 'ignored')", name="status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    current: Mapped[dict[str, Any]]
    proposed: Mapped[dict[str, Any]]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(String(8), default="open")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_at: Mapped[datetime | None]
