"""Chat knowledge tables: what Derp knows about each chat, always scoped by chat."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (
        CheckConstraint("type in ('private', 'group', 'supergroup', 'channel')", name="type"),
        CheckConstraint("notes_mode in ('auto', 'members', 'admins', 'off')", name="notes_mode"),
        CheckConstraint("retention_days is null or retention_days > 0", name="retention_days"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str | None]
    username: Mapped[str | None]
    is_forum: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Policy: ambient capture, notes mode, retention, locale, free-model fallback; null means "use the default for this chat type".
    locale: Mapped[str | None] = mapped_column(String(8))
    timezone: Mapped[str | None] = mapped_column(String(64))
    ambient: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    retention_days: Mapped[int | None]
    notes_mode: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    free_models: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Telegram-side state: getChat profile snapshot and Derp's own membership.
    profile: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    bot_status: Mapped[str | None] = mapped_column(String(16))
    onboarded_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    is_bot: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    first_name: Mapped[str] = mapped_column(default="", server_default="")
    last_name: Mapped[str | None]
    username: Mapped[str | None]
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_premium: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Free-model consent (free providers may store prompts): when and under which policy text version.
    free_models_consent_at: Mapped[datetime | None]
    free_models_consent_version: Mapped[str | None] = mapped_column(String(16))
    blocked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() if self.last_name else self.first_name
