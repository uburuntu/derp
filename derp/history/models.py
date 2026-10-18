"""Chat knowledge tables: what Derp knows about each chat, always scoped by chat."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Computed, ForeignKeyConstraint, Index, String, func
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

# ``message_media.transcript_model`` of a recording whose transcript is empty because none was made: why.
TOO_LONG = "too-long"  # over ``media.voice_max_seconds``: never transcribed
UNHEARD = "failed"  # the last attempt failed (download, ffmpeg, provider); an invocation may retry


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
    rules: Mapped[str | None]  # admin-written instructions; the only chat text Derp treats as trusted
    rules_draft: Mapped[str | None]  # model-worded rules waiting for the admin's confirmation
    rules_draft_by: Mapped[int | None]
    # Telegram-side state: getChat profile snapshot and Derp's own membership.
    profile: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=sql("'{}'::jsonb"))
    bot_status: Mapped[str | None] = mapped_column(String(16))
    onboarded_at: Mapped[datetime | None]
    blocked_at: Mapped[datetime | None]  # set by an owner: Derp answers nothing here
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def link_prefix(self) -> str | None:
        """Where this chat's message links start: by username in public chats, by internal id in private
        supergroups. Basic groups and private chats have no message links."""
        if self.type not in {"supergroup", "channel"}:
            return None
        if self.username:
            return f"https://t.me/{self.username}"
        digits = str(self.id)
        return f"https://t.me/c/{digits.removeprefix('-100')}" if digits.startswith("-100") else None

    def message_link(self, message_id: int) -> str | None:
        return f"{prefix}/{message_id}" if (prefix := self.link_prefix) else None


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    is_bot: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    first_name: Mapped[str] = mapped_column(default="", server_default="")
    last_name: Mapped[str | None]
    username: Mapped[str | None]
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_premium: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    timezone: Mapped[str | None] = mapped_column(String(64))  # unused; kept for expand-only schema
    # Free-model consent (free providers may store prompts): when and under which policy text version.
    free_models_consent_at: Mapped[datetime | None]
    free_models_consent_version: Mapped[str | None] = mapped_column(String(16))
    blocked_at: Mapped[datetime | None]  # set by an owner: Derp answers nothing they ask
    # Their own Derpcoins may pay in groups once the chat's run out (a personal /settings choice).
    group_spending: Mapped[bool] = mapped_column(default=True, server_default=sql("true"))
    # Named in the chat's thank-you post when they buy Derpcoins for a group (a personal choice).
    patron_named: Mapped[bool] = mapped_column(default=True, server_default=sql("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() if self.last_name else self.first_name

    @property
    def label(self) -> str:
        """How transcripts name the person: their name, then @username when they have one (writing it pings them)."""
        return f"{self.full_name} @{self.username}" if self.username else self.full_name


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
    file_id: Mapped[str]  # usable only by the bot that received it
    bot_id: Mapped[int]  # that bot
    file_unique_id: Mapped[str]  # the same for every bot; a key, never downloadable
    mime_type: Mapped[str | None]
    file_size: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    duration: Mapped[int | None]
    file_name: Mapped[str | None]
    sticker_emoji: Mapped[str | None]
    sticker_set: Mapped[str | None]
    # Telegram's preview picture (same bot): how a clip, an animated sticker or a file is seen when it isn't one.
    thumb_file_id: Mapped[str | None]
    thumb_unique_id: Mapped[str | None]
    transcript: Mapped[
        str | None
    ]  # None: not tried yet; "": tried, nothing (``transcript_model`` may say why)
    transcript_model: Mapped[str | None]  # the model, or why the transcript is empty: TOO_LONG / UNHEARD
    description: Mapped[str | None]
    description_run_id: Mapped[int | None]  # the run whose note it is; cleared with the note


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
    offered_on: Mapped[date | None]  # the last UTC day they got the out-of-Derpcoins offer here (money.free)
    waiting_message_id: Mapped[
        int | None
    ]  # their latest ask here nobody could pay for, until resumed (money.free)
    waiting_at: Mapped[datetime | None]


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


class Reaction(Base):
    """One actor's current reaction on a stored message; the actor's new list replaces the old one."""

    __tablename__ = "reactions"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    actor_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=False
    )  # user, or chat for anonymous
    reaction: Mapped[str] = mapped_column(String(64), primary_key=True)  # emoji, "custom:<id>" or "paid"
    actor_is_chat: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    set_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReactionCount(Base):
    """Anonymous reaction totals per message (Telegram sends these to admins, with a delay)."""

    __tablename__ = "reaction_counts"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    reaction: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Poll(Base):
    """A poll seen in a chat. Live results arrive only for Derp's own polls and stopped polls."""

    __tablename__ = "polls"
    __table_args__ = (
        CheckConstraint("type in ('regular', 'quiz')", name="type"),
        CheckConstraint("origin in ('derp_poll', 'derp_quiz', 'user')", name="origin"),
        Index("ix_polls_chat_message", "chat_id", "message_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[int]
    message_id: Mapped[int]
    question: Mapped[str]
    options: Mapped[list[Any]]  # [{"id": persistent_id, "text": ..., "voters": n}]
    total_voters: Mapped[int] = mapped_column(default=0, server_default="0")
    type: Mapped[str] = mapped_column(String(8))
    correct_option_ids: Mapped[list[Any] | None]
    explanation: Mapped[str | None]
    is_anonymous: Mapped[bool]
    multiple: Mapped[bool]
    closed: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    close_date: Mapped[datetime | None]
    origin: Mapped[str] = mapped_column(String(9))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PollVote(Base):
    """A voter's current choice in one of Derp's non-anonymous polls; a retracted vote is an empty list."""

    __tablename__ = "poll_votes"

    poll_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    voter_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)  # user, or chat
    option_ids: Mapped[list[Any]]
    option_persistent_ids: Mapped[list[Any]]  # stable when options are added later
    voted_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class GameScore(Base):
    """Quiz points per member and chat-local day: +1 for a right answer in Derp's quizzes, −1 for a wrong one.

    The plain sum, so answer order doesn't matter; a day counts as at least 0 where totals are read.
    """

    __tablename__ = "game_scores"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    day: Mapped[date] = mapped_column(primary_key=True)
    points: Mapped[int] = mapped_column(default=0, server_default="0")
    answers: Mapped[int] = mapped_column(default=0, server_default="0")


class DailyTitle(Base):
    """One "<title> of the day" winner per chat, title (normalized) and chat-local day; counted for leaderboards."""

    __tablename__ = "daily_titles"
    __table_args__ = (Index("ix_daily_titles_chat_user", "chat_id", "user_id"),)

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Sticker(Base):
    """A sticker a bot has seen anywhere (public assets, no sender kept); the pool ``send_sticker`` draws from.

    Keyed per bot: ``file_id`` works only for the bot that received it, while ``file_unique_id`` is shared.
    """

    __tablename__ = "stickers"
    __table_args__ = (Index("ix_stickers_bot_emoji", "bot_id", "emoji"),)

    bot_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    file_unique_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str]
    emoji: Mapped[str | None] = mapped_column(String(16))
    set_name: Mapped[str | None]
    kind: Mapped[str] = mapped_column(String(12), default="regular")  # regular | mask | custom_emoji
    curated: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    uses: Mapped[int] = mapped_column(default=1, server_default="1")
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class Digest(Base):
    """A summary of one chat day (in the chat's time zone), built on first request and cached.

    ``thread_id`` 0 is the whole chat; otherwise one forum topic. ``last_message_id``
    is the newest message the day had when it was built, so today's digest is
    rebuilt once newer messages exist. Edits inside the day mark it ``stale``;
    forgetting a message it covers deletes it.
    """

    __tablename__ = "digests"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    thread_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    day: Mapped[date] = mapped_column(primary_key=True)
    first_message_id: Mapped[int]
    last_message_id: Mapped[int]
    message_count: Mapped[int]
    summary: Mapped[str]
    model: Mapped[str] = mapped_column(String(120))
    stale: Mapped[bool] = mapped_column(default=False, server_default=sql("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
