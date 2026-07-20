"""Short database commands for context policy and ambient retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.policy import CONTEXT_NOTICE_VERSION
from derp.models import Chat, Message, User

_TOMBSTONE_TEXT = "[message deleted]"
_TOMBSTONE_HISTORY: dict[str, object] = {
    "schema_version": 1,
    "kind": "user_text",
    "speaker": {"id": None, "display_name": "Deleted"},
    "text": _TOMBSTONE_TEXT,
    "attachments": [],
    "tombstone": True,
}
_TOMBSTONE_CANONICAL: dict[str, object] = {
    "schema_version": 1,
    "type": "tombstone",
    "text": _TOMBSTONE_TEXT,
    "attachment_types": [],
}


async def lock_chat_history_policy(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
) -> Chat | None:
    """Serialize capture, policy mutation, and destructive history commands."""
    result = await session.execute(
        select(Chat).where(Chat.telegram_id == chat_telegram_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def purge_expired_history(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    chat_telegram_id: int | None = None,
) -> int:
    """Hard-delete source data after its disclosed retention deadline."""
    conditions = [Message.retention_expires_at <= (now or datetime.now(UTC))]
    if chat_telegram_id is not None:
        conditions.append(
            Message.chat_id.in_(
                select(Chat.id).where(Chat.telegram_id == chat_telegram_id)
            )
        )
    result = await session.execute(delete(Message).where(*conditions))
    return result.rowcount or 0


async def acknowledge_context_notice(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    ambient_enabled: bool,
) -> None:
    """Record a delivered notice and the truthful initial capture state."""
    await session.execute(
        update(Chat)
        .where(Chat.telegram_id == chat_telegram_id)
        .values(
            context_notice_version=CONTEXT_NOTICE_VERSION,
            ambient_history_enabled=ambient_enabled,
            updated_at=datetime.now(UTC),
        )
    )


async def set_ambient_history(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    enabled: bool,
) -> int:
    """Set capture policy and purge ambient-only history when disabling it."""
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        raise LookupError(f"Unknown Telegram chat: {chat_telegram_id}")
    chat.ambient_history_enabled = enabled
    chat.context_notice_version = CONTEXT_NOTICE_VERSION
    chat.updated_at = datetime.now(UTC)
    if enabled:
        return 0
    deleted = await session.execute(
        delete(Message).where(
            Message.chat_id.in_(
                select(Chat.id).where(Chat.telegram_id == chat_telegram_id)
            ),
            Message.capture_kind == "ambient",
        )
    )
    return deleted.rowcount or 0


async def set_history_retention(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    retention_days: int,
) -> None:
    """Set retention, rebase live rows, and immediately purge expired data."""
    if retention_days not in {7, 30, 90}:
        raise ValueError("Retention must be 7, 30, or 90 days")
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        raise LookupError(f"Unknown Telegram chat: {chat_telegram_id}")
    timestamp = datetime.now(UTC)
    chat.retention_days = retention_days
    chat.updated_at = timestamp
    await session.execute(
        update(Message)
        .where(
            Message.chat_id == chat.id,
            Message.privacy_deleted_at.is_(None),
        )
        .values(
            retention_expires_at=(
                Message.telegram_date + timedelta(days=retention_days)
            ),
            updated_at=timestamp,
        )
    )
    await purge_expired_history(
        session,
        now=timestamp,
        chat_telegram_id=chat_telegram_id,
    )


async def remove_disqualified_message(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    telegram_message_id: int,
) -> bool:
    """Remove a stored projection when an edit no longer qualifies for capture."""
    await lock_chat_history_policy(session, chat_telegram_id=chat_telegram_id)
    result = await session.execute(
        delete(Message).where(
            Message.chat_id.in_(
                select(Chat.id).where(Chat.telegram_id == chat_telegram_id)
            ),
            Message.telegram_message_id == telegram_message_id,
        )
    )
    return bool(result.rowcount)


async def tombstone_user_messages(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    actor_telegram_id: int,
    telegram_message_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Anonymize one or all live messages owned by an actor in a chat."""
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        return 0
    timestamp = now or datetime.now(UTC)
    owned = (
        select(Message.id)
        .join(User, Message.user_id == User.id)
        .where(
            Message.chat_id == chat.id,
            User.telegram_id == actor_telegram_id,
            Message.direction == "in",
            Message.privacy_deleted_at.is_(None),
            Message.retention_expires_at > timestamp,
        )
    )
    if telegram_message_id is not None:
        owned = owned.where(Message.telegram_message_id == telegram_message_id)
    result = await session.execute(
        update(Message)
        .where(Message.id.in_(owned))
        .values(
            user_id=None,
            content_type="deleted",
            text=_TOMBSTONE_TEXT,
            source_snapshot={},
            history_dto=_TOMBSTONE_HISTORY,
            canonical_projection=_TOMBSTONE_CANONICAL,
            media_group_id=None,
            attachment_type=None,
            attachment_file_id=None,
            privacy_deleted_at=timestamp,
            updated_at=timestamp,
        )
    )
    return result.rowcount or 0


async def clear_history_scope(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    thread_id: int | None,
) -> int:
    """Purge the current chat/topic scope without touching approved facts."""
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        return 0
    scope = (
        Message.thread_id.is_(None)
        if thread_id is None
        else Message.thread_id == thread_id
    )
    result = await session.execute(
        delete(Message).where(Message.chat_id == chat.id, scope)
    )
    return result.rowcount or 0


async def claim_member_notice(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    now: datetime | None = None,
    interval: timedelta = timedelta(hours=12),
) -> bool:
    """Atomically rate-limit disclosure for a batch of new group members."""
    timestamp = now or datetime.now(UTC)
    result = await session.execute(
        update(Chat)
        .where(
            Chat.telegram_id == chat_telegram_id,
            Chat.ambient_history_enabled.is_(True),
            Chat.context_notice_version >= CONTEXT_NOTICE_VERSION,
            or_(
                Chat.member_notice_sent_at.is_(None),
                Chat.member_notice_sent_at <= timestamp - interval,
            ),
        )
        .values(member_notice_sent_at=timestamp, updated_at=timestamp)
        .returning(Chat.id)
    )
    return result.scalar_one_or_none() is not None


__all__ = [
    "acknowledge_context_notice",
    "clear_history_scope",
    "claim_member_notice",
    "lock_chat_history_policy",
    "purge_expired_history",
    "remove_disqualified_message",
    "set_ambient_history",
    "set_history_retention",
    "tombstone_user_messages",
]
