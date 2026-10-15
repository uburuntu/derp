"""Chat knowledge tables: what Derp knows about each chat, always scoped by chat."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Computed, ForeignKeyConstraint, Index, String, func
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import TSVECTOR
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
    is_forum: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    # Policy: ambient capture, notes mode, retention, locale, free-model fallback; null means "use the default for this chat type".
    locale: Mapped[str | None] = mapped_column(String(8))
    timezone: Mapped[str | None] = mapped_column(String(64))
    ambient: Mapped[bool] = mapped_column(default=True, server_default=sql("true"))
    retention_days: Mapped[int | None]
    notes_mode: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    free_models: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    # Telegram-side state: getChat profile snapshot and Derp's own membership.
    profile: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=sql("'{}'::jsonb"))
    bot_status: Mapped[str | None] = mapped_column(String(16))
    onboarded_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    is_bot: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    first_name: Mapped[str] = mapped_column(default="", server_default="")
    last_name: Mapped[str | None]
    username: Mapped[str | None]
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_premium: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
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


class ForumTopic(Base):
    __tablename__ = "forum_topics"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    thread_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(default="")
    icon_custom_emoji_id: Mapped[str | None]
    closed: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


MESSAGE_KINDS = (
    "text", "photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation",
    "poll", "dice", "location", "venue", "contact", "checklist", "story", "game", "invoice",
    "giveaway", "paid_media", "service", "other",
)  # fmt: skip


class Message(Base):
    """One Telegram message Derp saw or sent. ``text`` is the text or caption."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("kind in (" + ", ".join(f"'{k}'" for k in MESSAGE_KINDS) + ")", name="kind"),
        CheckConstraint("capture in ('ambient', 'invocation', 'derp')", name="capture"),
        CheckConstraint("message_id > 0", name="message_id_positive"),
        Index("ix_messages_chat_thread_date", "chat_id", "thread_id", "date"),
        Index("ix_messages_chat_user_date", "chat_id", "user_id", "date"),
        Index("ix_messages_date", "date"),
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_messages_text_trgm", "text", postgresql_using="gin", postgresql_ops={"text": "gin_trgm_ops"}
        ),
    )

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    thread_id: Mapped[int | None]  # forum topic; null for the General topic and non-forum chats
    user_id: Mapped[int | None]
    sender_chat_id: Mapped[int | None]
    date: Mapped[datetime]
    edit_date: Mapped[datetime | None]
    kind: Mapped[str] = mapped_column(String(12))
    text: Mapped[str | None]
    entities: Mapped[list[Any] | None]
    reply_to_message_id: Mapped[int | None]
    quote: Mapped[str | None]
    forward_origin: Mapped[dict[str, Any] | None]
    media_group_id: Mapped[str | None]
    via_bot_id: Mapped[int | None]
    capture: Mapped[str] = mapped_column(String(10))
    is_derp: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=sql("'{}'::jsonb"))
    raw: Mapped[dict[str, Any] | None]
    # Searchable text derived from media: voice transcripts and media descriptions.
    extra_text: Mapped[str | None]
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(text, '') || ' ' || coalesce(extra_text, ''))", persisted=True
        ),
    )
    deleted_at: Mapped[datetime | None]  # tombstone from privacy actions or Derp's own deletions


class MessageMedia(Base):
    __tablename__ = "message_media"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chat_id", "message_id"], ["messages.chat_id", "messages.message_id"], ondelete="CASCADE"
        ),
    )

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    idx: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, default=0)
    type: Mapped[str] = mapped_column(String(12))
    file_id: Mapped[str]
    file_unique_id: Mapped[str]
    mime_type: Mapped[str | None]
    file_size: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    duration: Mapped[int | None]
    file_name: Mapped[str | None]
    sticker_emoji: Mapped[str | None]
    sticker_set: Mapped[str | None]
    transcript: Mapped[str | None]
    transcript_model: Mapped[str | None]
    description: Mapped[str | None]
    description_run_id: Mapped[int | None]


class ChatMember(Base):
    """A person seen in a chat: status, activity and the names they used there."""

    __tablename__ = "chat_members"
    __table_args__ = (
        CheckConstraint(
            "status in ('creator', 'administrator', 'member', 'restricted', 'left', 'kicked')", name="status"
        ),
    )

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    status: Mapped[str] = mapped_column(String(13), default="member")
    tag: Mapped[str | None]  # admin custom title or member tag
    names: Mapped[list[Any]] = mapped_column(default=list, server_default=sql("'[]'::jsonb"))
    message_count: Mapped[int] = mapped_column(default=0, server_default="0")
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class MemberEvent(Base):
    __tablename__ = "member_events"
    __table_args__ = (
        CheckConstraint(
            "kind in ('join', 'leave', 'promote', 'demote', 'ban', 'unban', 'restrict', 'title')", name="kind"
        ),
        Index("ix_member_events_chat_at", "chat_id", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    user_id: Mapped[int]
    kind: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[int | None]
    at: Mapped[datetime] = mapped_column(server_default=func.now())
