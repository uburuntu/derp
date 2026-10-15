"""Notes Derp keeps per chat: chat notes and each member's "my notes" in that chat."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base


class Note(Base):
    """One remembered fact. ``user_id`` set → that member's own note in this chat; never shared across chats."""

    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint("state in ('active', 'proposed')", name="state"),
        CheckConstraint("char_length(text) between 1 and 500", name="text_length"),
        Index("ix_notes_chat_user", "chat_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    user_id: Mapped[int | None]
    text: Mapped[str]
    author_id: Mapped[int | None]  # who asked for it (or whose message Derp noted)
    state: Mapped[str] = mapped_column(String(8), default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
