"""Notes Derp keeps per chat: chat notes and each member's "my notes" in that chat."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base


class Note(Base):
    """One remembered fact. ``user_id`` set → that member's own note in this chat; never shared across chats."""

    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint("state in ('active', 'proposed')", name="state"),
        CheckConstraint("char_length(text) between 1 and 1000", name="text_length"),
        Index("ix_notes_chat_user", "chat_id", "user_id"),
        Index("ix_notes_replaces_id", "replaces_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    user_id: Mapped[int | None]
    text: Mapped[str]
    author_id: Mapped[int | None]  # who asked for it (or whose message Derp noted)
    state: Mapped[str] = mapped_column(String(8), default="active")
    # A proposed edit: the active note it rewrites once an admin approves (gone with that note).
    replaces_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ReplyRule(Base):
    """A group admin's confirmed condition and action, or the exact draft shown for approval."""

    __tablename__ = "reply_rules"
    __table_args__ = (
        CheckConstraint("state in ('draft', 'active', 'paused')", name="state"),
        Index("ix_reply_rules_chat_state", "chat_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    admin_id: Mapped[int]
    thread_id: Mapped[int | None]  # None can also mean General; whole_chat distinguishes it.
    whole_chat: Mapped[bool] = mapped_column(default=False)
    sender_id: Mapped[int | None]
    sender_name: Mapped[str | None]
    condition: Mapped[str]
    action: Mapped[str]
    needs_media: Mapped[bool] = mapped_column(default=False)
    grants: Mapped[list[dict[str, Any]]] = mapped_column("outputs", JSONB, default=list)
    state: Mapped[str] = mapped_column(String(8), default="draft")
    replaces_id: Mapped[int | None] = mapped_column(ForeignKey("reply_rules.id", ondelete="CASCADE"))


class ReplyRuleCheck(Base):
    """Claim before any paid check; retained with its source, including nonmatches and ambiguous failures."""

    __tablename__ = "reply_rule_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chat_id", "message_id"], ["messages.chat_id", "messages.message_id"], ondelete="CASCADE"
        ),
    )

    chat_id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(primary_key=True)
    matched: Mapped[list[int]] = mapped_column(JSONB, default=list)
