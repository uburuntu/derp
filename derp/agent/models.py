"""Agent runs and what they delivered."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

# Runs end completed, failed or cancelled. "awaiting_approval", "denied" and "expired" are never written
# (approvals keep their own state) but stay allowed: the CHECK constraint is built from this list.
RUN_STATUSES = ("running", "awaiting_approval", "completed", "failed", "cancelled", "denied", "expired")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("status in (" + ", ".join(f"'{s}'" for s in RUN_STATUSES) + ")", name="status"),
        CheckConstraint(
            "trigger in ('message', 'command', 'job', 'inline', 'guest', 'callback')", name="trigger"
        ),
        CheckConstraint("charged_credits >= 0 and cost_usd >= 0", name="money_nonnegative"),
        Index("ix_agent_runs_chat_id_created_at", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    thread_id: Mapped[int | None]
    user_id: Mapped[int]  # the invoker; scheduled jobs use the job's creator
    trigger: Mapped[str] = mapped_column(String(10))
    trigger_message_id: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(20), default="running")
    role: Mapped[str] = mapped_column(String(16))
    funding: Mapped[str] = mapped_column(String(12))  # paid | sponsored | free
    wallet_id: Mapped[int | None]
    reservation_id: Mapped[int | None]
    served_model: Mapped[str | None] = mapped_column(String(128))
    context_tokens: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    reasoning_tokens: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=Decimal(0))
    charged_credits: Mapped[int] = mapped_column(default=0)
    # Pydantic AI message history of this run (binary content replaced by markers); follows chat retention.
    messages: Mapped[list[Any] | None]
    error_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


class RunOutput(Base):
    """Every visible thing a run delivered, so ``/info`` works on any of them."""

    __tablename__ = "run_outputs"
    __table_args__ = (
        CheckConstraint(
            "kind in ('message', 'image', 'poll', 'sticker', 'dice', 'reaction', 'invoice', 'other')",
            name="kind",
        ),
        Index("ix_run_outputs_chat_id_message_id", "chat_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int]
    message_id: Mapped[int]  # for reactions: the message that was reacted to
    kind: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Approval(Base):
    """An expensive action waiting for its requester's Make it or Cancel; denial and expiry cost nothing."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("state in ('pending', 'approved', 'denied', 'expired')", name="state"),
        CheckConstraint("kind in ('image')", name="kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None]  # the run that asked
    chat_id: Mapped[int]
    thread_id: Mapped[int | None]
    user_id: Mapped[int]  # only they can decide
    reply_to: Mapped[int | None]  # the message the result answers
    kind: Mapped[str] = mapped_column(String(12))
    args: Mapped[dict[str, Any]]  # what to run (prompt text: deleted with the row after a day)
    credits: Mapped[int]  # the estimate shown on the prompt
    state: Mapped[str] = mapped_column(String(10), default="pending")
    prompt_message_id: Mapped[int | None]
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
