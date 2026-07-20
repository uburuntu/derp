"""Untrusted shared facts proposed and reviewed within one chat scope."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from derp.models.chat import Chat
    from derp.models.user import User

SHARED_FACT_MAX_LENGTH = 1024


class SharedFactState(StrEnum):
    """Review lifecycle for untrusted factual memory."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class SharedFact(TimestampMixin, Base):
    """A member-proposed fact that never carries instruction authority."""

    __tablename__ = "shared_facts"
    __table_args__ = (
        CheckConstraint(
            "state::text = ANY (ARRAY['proposed'::text, 'approved'::text, "
            "'rejected'::text])",
            name="shared_fact_state_allowed",
        ),
        CheckConstraint(
            "length(fact_text) <= 1024",
            name="shared_fact_text_max_length",
        ),
        CheckConstraint(
            "length(btrim(fact_text)) > 0",
            name="shared_fact_text_not_blank",
        ),
        CheckConstraint(
            "thread_id IS NULL OR thread_id > 0",
            name="shared_fact_thread_id_positive",
        ),
        CheckConstraint(
            "state::text = 'proposed'::text AND decided_by_user_id IS NULL "
            "AND decided_at IS NULL OR "
            "(state::text = ANY (ARRAY['approved'::text, 'rejected'::text])) "
            "AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL",
            name="shared_fact_decision_complete",
        ),
        Index(
            "idx_shared_facts_scope_approved",
            "chat_id",
            "thread_id",
            "created_at",
            "id",
            postgresql_where="state = 'approved'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE")
    )
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fact_text: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(16),
        default=SharedFactState.PROPOSED.value,
        server_default=text("'proposed'"),
    )
    proposed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    chat: Mapped[Chat] = relationship(back_populates="shared_facts")
    proposer: Mapped[User] = relationship(
        back_populates="proposed_shared_facts",
        foreign_keys=[proposed_by_user_id],
    )
    decider: Mapped[User | None] = relationship(
        back_populates="decided_shared_facts",
        foreign_keys=[decided_by_user_id],
    )

    @property
    def is_approved(self) -> bool:
        """Return whether the fact is available to conversation assembly."""
        return self.state == SharedFactState.APPROVED


__all__ = ["SHARED_FACT_MAX_LENGTH", "SharedFact", "SharedFactState"]
