"""Ops tables: product feedback people leave for the owners."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

# A report on one answer (/info's Report a problem) is one of REPORT_KINDS; "bug" is shown as "Broken".
REPORT_KINDS = ("wrong_answer", "missed_context", "wrong_image", "bug")
FEEDBACK_KINDS = ("bug", "idea", "praise", "other", "wrong_answer", "missed_context", "wrong_image")


class Feedback(Base):
    """A bug report, wish or kind word for the owners, from /feedback, filed by Derp on request, or a
    report on one answer from /info (its run and message)."""

    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(
            "kind in ('bug', 'idea', 'praise', 'other', 'wrong_answer', 'missed_context', 'wrong_image')",
            name="kind",
        ),
        CheckConstraint("state in ('open', 'done')", name="state"),
        Index("ix_feedback_state_id", "state", "id"),
        Index("ix_feedback_details", "user_id", postgresql_where="details_until is not null"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    chat_id: Mapped[int]
    message_id: Mapped[int | None]  # the message it came from (a report: the answer), when stored
    run_id: Mapped[int | None]  # set when Derp filed it, or the reported answer's run
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str]
    details_until: Mapped[datetime | None]  # a report's author may add a line until then (their next message)
    state: Mapped[str] = mapped_column(String(4), default="open", server_default="open")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
