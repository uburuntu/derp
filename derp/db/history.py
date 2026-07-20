"""Short database commands for context policy and ambient retention."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
from sqlalchemy import ARRAY, Text, cast, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from derp.history.policy import CONTEXT_NOTICE_VERSION, ChatPolicyFlag
from derp.models import Chat, DeferredToolRequest, Message, User

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
_ACTIVE_DEFERRED_STATUSES = ("pending", "approved")


async def _scrub_deferred_requests_for_messages(
    session: AsyncSession,
    message_ids: Select[tuple[uuid.UUID]],
    *,
    now: datetime,
) -> int:
    """Expire approvals whose durable history may contain selected messages."""
    scopes = (
        select(Message.chat_id, Message.thread_id)
        .where(Message.id.in_(message_ids))
        .distinct()
        .subquery()
    )
    matching_scope = exists(
        select(1).where(
            scopes.c.chat_id == DeferredToolRequest.chat_id,
            scopes.c.thread_id.is_not_distinct_from(DeferredToolRequest.thread_id),
        )
    )
    result = await session.execute(
        update(DeferredToolRequest)
        .where(
            DeferredToolRequest.status.in_(_ACTIVE_DEFERRED_STATUSES),
            matching_scope,
        )
        .values(
            status="expired",
            validated_arguments={},
            original_history=[],
            decided_at=now,
            resumed_at=None,
            updated_at=now,
        )
    )
    return result.rowcount or 0


async def _scrub_deferred_requests_for_scope(
    session: AsyncSession,
    *,
    chat_id: uuid.UUID,
    thread_id: int | None,
    now: datetime,
) -> int:
    scope = (
        DeferredToolRequest.thread_id.is_(None)
        if thread_id is None
        else DeferredToolRequest.thread_id == thread_id
    )
    result = await session.execute(
        update(DeferredToolRequest)
        .where(
            DeferredToolRequest.chat_id == chat_id,
            scope,
            DeferredToolRequest.status.in_(_ACTIVE_DEFERRED_STATUSES),
        )
        .values(
            status="expired",
            validated_arguments={},
            original_history=[],
            decided_at=now,
            resumed_at=None,
            updated_at=now,
        )
    )
    return result.rowcount or 0


class _ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tool_name: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    arguments_json: str

    @field_validator("arguments_json")
    @classmethod
    def validate_arguments_json(cls, value: str) -> str:
        try:
            arguments = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Tool arguments must be valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must serialize an object")
        return value


class _ToolResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tool_name: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    content: str


class _ToolRoundRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    called_at: str = Field(min_length=1)
    returned_at: str = Field(min_length=1)
    calls: list[_ToolCallRecord] = Field(min_length=1)
    results: list[_ToolResultRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_round(self) -> Self:
        calls = [(call.tool_name, call.tool_call_id) for call in self.calls]
        results = [(result.tool_name, result.tool_call_id) for result in self.results]
        if calls != results:
            raise ValueError("Tool calls and results must form complete ordered pairs")
        called_at = _parse_transcript_timestamp(self.called_at)
        returned_at = _parse_transcript_timestamp(self.returned_at)
        if returned_at < called_at:
            raise ValueError("Tool result timestamp must not precede its call")
        return self


_TOOL_ROUNDS_ADAPTER = TypeAdapter(list[_ToolRoundRecord])


def _parse_transcript_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Tool transcript timestamps must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Tool transcript timestamps must include a timezone")
    return parsed


def _validate_tool_rounds(
    tool_rounds: list[dict[str, object]],
) -> list[dict[str, object]]:
    rounds = _TOOL_ROUNDS_ADAPTER.validate_python(tool_rounds, strict=True)
    call_ids: set[str] = set()
    for round_ in rounds:
        for call in round_.calls:
            if call.tool_call_id in call_ids:
                raise ValueError("Tool call IDs must be unique across the transcript")
            call_ids.add(call.tool_call_id)
    return [round_.model_dump(mode="json") for round_ in rounds]


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
    timestamp = now or datetime.now(UTC)
    conditions = [Message.retention_expires_at <= timestamp]
    if chat_telegram_id is not None:
        conditions.append(
            Message.chat_id.in_(
                select(Chat.id).where(Chat.telegram_id == chat_telegram_id)
            )
        )
    message_ids = select(Message.id).where(*conditions)
    await _scrub_deferred_requests_for_messages(
        session,
        message_ids,
        now=timestamp,
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
    ambient_ids = select(Message.id).where(
        Message.chat_id == chat.id,
        Message.capture_kind == "ambient",
    )
    await _scrub_deferred_requests_for_messages(
        session,
        ambient_ids,
        now=chat.updated_at,
    )
    deleted = await session.execute(
        delete(Message).where(
            Message.chat_id == chat.id,
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


async def set_admin_policy(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    policy: str | None,
) -> str | None:
    """Store one bounded admin-authored instruction paragraph."""
    normalized = policy.strip() if policy else None
    if normalized and len(normalized) > 2048:
        raise ValueError("Admin policy must be at most 2048 characters")
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        raise LookupError(f"Unknown Telegram chat: {chat_telegram_id}")
    chat.admin_policy = normalized or None
    chat.updated_at = datetime.now(UTC)
    await session.flush()
    return chat.admin_policy


async def set_chat_policy_flag(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    flag: ChatPolicyFlag,
    enabled: bool,
) -> None:
    """Mutate one allowlisted typed policy flag under the chat policy lock."""
    if not isinstance(flag, ChatPolicyFlag):
        raise TypeError("flag must be a ChatPolicyFlag")
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        raise LookupError(f"Unknown Telegram chat: {chat_telegram_id}")
    setattr(chat, flag.value, enabled)
    chat.updated_at = datetime.now(UTC)
    await session.flush()


async def remove_disqualified_message(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    telegram_message_id: int,
) -> bool:
    """Remove a stored projection when an edit no longer qualifies for capture."""
    await lock_chat_history_policy(session, chat_telegram_id=chat_telegram_id)
    timestamp = datetime.now(UTC)
    conditions = (
        Message.chat_id.in_(
            select(Chat.id).where(Chat.telegram_id == chat_telegram_id)
        ),
        Message.telegram_message_id == telegram_message_id,
    )
    await _scrub_deferred_requests_for_messages(
        session,
        select(Message.id).where(*conditions),
        now=timestamp,
    )
    result = await session.execute(delete(Message).where(*conditions))
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
    await _scrub_deferred_requests_for_messages(
        session,
        owned,
        now=timestamp,
    )
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
    await _scrub_deferred_requests_for_scope(
        session,
        chat_id=chat.id,
        thread_id=thread_id,
        now=datetime.now(UTC),
    )
    result = await session.execute(
        delete(Message).where(Message.chat_id == chat.id, scope)
    )
    return result.rowcount or 0


async def store_tool_transcript(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    telegram_message_id: int,
    tool_rounds: list[dict[str, object]],
    now: datetime | None = None,
) -> bool:
    """Atomically attach complete tool rounds to one live inbound request."""
    serialized_rounds = _validate_tool_rounds(tool_rounds)
    timestamp = now or datetime.now(UTC)
    chat = await lock_chat_history_policy(
        session,
        chat_telegram_id=chat_telegram_id,
    )
    if chat is None:
        return False

    result = await session.execute(
        update(Message)
        .where(
            Message.chat_id == chat.id,
            Message.telegram_message_id == telegram_message_id,
            Message.direction == "in",
            Message.role == "user",
            Message.deleted_at.is_(None),
            Message.privacy_deleted_at.is_(None),
            Message.retention_expires_at > timestamp,
            func.jsonb_typeof(Message.history_dto) == "object",
        )
        .values(
            history_dto=func.jsonb_set(
                Message.history_dto,
                cast(["tool_rounds"], ARRAY(Text)),
                cast(serialized_rounds, JSONB),
                True,
            ),
            updated_at=timestamp,
        )
        .returning(Message.id)
    )
    return result.scalar_one_or_none() is not None


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
    "set_admin_policy",
    "set_ambient_history",
    "set_chat_policy_flag",
    "set_history_retention",
    "store_tool_transcript",
    "tombstone_user_messages",
]
