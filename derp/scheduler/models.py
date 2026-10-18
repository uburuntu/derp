"""Scheduled jobs: a reminder text or a prompt Derp answers at a set time, once or repeating."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

REPEATS = ("daily", "weekdays", "weekly", "monthly")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("kind in ('message', 'prompt')", name="kind"),
        CheckConstraint("state in ('active', 'paused', 'done', 'cancelled')", name="state"),
        CheckConstraint(
            "repeat is null or repeat in (" + ", ".join(f"'{r}'" for r in REPEATS) + ")", name="repeat"
        ),
        CheckConstraint("char_length(text) between 1 and 1000", name="text_length"),
        Index("ix_jobs_state_next_at", "state", "next_at"),
        Index("ix_jobs_chat_id_state", "chat_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    thread_id: Mapped[int | None]
    creator_id: Mapped[int]  # the invoker when it fires: their funding pays for prompts
    kind: Mapped[str] = mapped_column(String(8))  # message: post the text | prompt: Derp answers it
    text: Mapped[str]
    repeat: Mapped[str | None] = mapped_column(String(8))  # wall-clock time in the chat's time zone
    next_at: Mapped[datetime]
    anchor_at: Mapped[datetime]  # the first scheduled time: repeats keep its wall time and day of month
    state: Mapped[str] = mapped_column(String(10), default="active")
    misses: Mapped[int] = mapped_column(default=0)  # unfunded firings in a row; pauses at the limit
    runs: Mapped[int] = mapped_column(default=0)
    last_fired_at: Mapped[datetime | None]
    # a one-off snoozed off a repeating job's firing: the series' one extra firing, moved by later snoozes
    snooze_of: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
