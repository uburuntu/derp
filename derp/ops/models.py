"""Ops tables: product feedback people leave for the owners."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

FEEDBACK_KINDS = ("bug", "idea", "praise", "other")


class Feedback(Base):
    """A bug report, wish or kind word for the owners, from /feedback or filed by Derp on request."""

    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("kind in ('bug', 'idea', 'praise', 'other')", name="kind"),
        CheckConstraint("state in ('open', 'done')", name="state"),
        Index("ix_feedback_state_id", "state", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    chat_id: Mapped[int]
    message_id: Mapped[int | None]  # the message it came from, when stored
    run_id: Mapped[int | None]  # set when Derp filed it
    kind: Mapped[str] = mapped_column(String(8))
    text: Mapped[str]
    state: Mapped[str] = mapped_column(String(4), default="open", server_default="open")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
