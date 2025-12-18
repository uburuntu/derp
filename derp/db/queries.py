"""Database queries for PostgreSQL.

This module provides typed async query functions for all database operations.
Queries are optimized for parallel execution with minimal round-trips.
"""

from __future__ import annotations

from datetime import UTC, datetime

import logfire
from sqlalchemy import ScalarSelect, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from derp.models import Chat, Message, User

# -----------------------------------------------------------------------------
# Subquery Helpers (for single-query upserts)
# -----------------------------------------------------------------------------


def _chat_id_subquery(telegram_id: int) -> ScalarSelect[int]:
    """Subquery to get chat.id from telegram_id (avoids extra round-trip)."""
    return select(Chat.id).where(Chat.telegram_id == telegram_id).scalar_subquery()


def _user_id_subquery(telegram_id: int) -> ScalarSelect[int]:
    """Subquery to get user.id from telegram_id (avoids extra round-trip)."""
    return select(User.id).where(User.telegram_id == telegram_id).scalar_subquery()


# -----------------------------------------------------------------------------
# User Queries
# -----------------------------------------------------------------------------


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    is_bot: bool,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
    language_code: str | None = None,
    is_premium: bool = False,
) -> User:
    """Upsert a Telegram user by telegram_id.

    Creates a new user or updates existing user data.
    Returns the User model instance.
    """
    stmt = insert(User).values(
        telegram_id=telegram_id,
        is_bot=is_bot,
        first_name=first_name,
        last_name=last_name,
        username=username,
        language_code=language_code,
        is_premium=is_premium,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={
            "is_bot": stmt.excluded.is_bot,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
            "username": stmt.excluded.username,
            "language_code": stmt.excluded.language_code,
            "is_premium": stmt.excluded.is_premium,
            "updated_at": datetime.now(UTC),
        },
    ).returning(User)

    result = await session.execute(stmt)
    return result.scalar_one()


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> User | None:
    """Get a user by their Telegram ID."""
    stmt = select(User).where(User.telegram_id == telegram_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# -----------------------------------------------------------------------------
# Chat Queries
# -----------------------------------------------------------------------------


async def upsert_chat(
    session: AsyncSession,
    *,
    telegram_id: int,
    chat_type: str,
    title: str | None = None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    is_forum: bool = False,
) -> Chat:
    """Upsert a Telegram chat by telegram_id.

    Creates a new chat or updates existing chat data.
    Returns the Chat model instance.
    """
    stmt = insert(Chat).values(
        telegram_id=telegram_id,
        type=chat_type,
        title=title,
        username=username,
        first_name=first_name,
        last_name=last_name,
        is_forum=is_forum,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Chat.telegram_id],
        set_={
            "type": stmt.excluded.type,
            "title": stmt.excluded.title,
            "username": stmt.excluded.username,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
            "is_forum": stmt.excluded.is_forum,
            "updated_at": datetime.now(UTC),
        },
    ).returning(Chat)

    result = await session.execute(stmt)
    return result.scalar_one()


async def get_chat_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> Chat | None:
    """Get a chat by its Telegram ID."""
    stmt = select(Chat).where(Chat.telegram_id == telegram_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_chat_settings(session: AsyncSession, telegram_id: int) -> Chat | None:
    """Get chat with settings by Telegram ID.

    Returns the Chat which includes llm_memory.
    """
    return await get_chat_by_telegram_id(session, telegram_id)


async def update_chat_memory(
    session: AsyncSession, telegram_id: int, llm_memory: str | None
) -> None:
    """Update the LLM memory for a chat."""
    stmt = (
        update(Chat)
        .where(Chat.telegram_id == telegram_id)
        .values(llm_memory=llm_memory, updated_at=datetime.now(UTC))
    )
    await session.execute(stmt)


# -----------------------------------------------------------------------------
# Message Queries
# -----------------------------------------------------------------------------


async def upsert_message(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    user_telegram_id: int | None,
    telegram_message_id: int,
    thread_id: int | None,
    direction: str,
    content_type: str | None,
    text: str | None,
    media_group_id: str | None = None,
    attachment_type: str | None = None,
    attachment_file_id: str | None = None,
    reply_to_message_id: int | None = None,
    telegram_date: datetime,
    edited_at: datetime | None = None,
) -> Message | None:
    """Upsert a message into the messages table.

    Uses chat_id + telegram_message_id as the natural key. Optimized to use
    subqueries for chat/user lookup, reducing 3 queries to 1.
    Returns the Message model instance or None if insert failed.
    """
    # Use subqueries to resolve IDs inline (single round-trip)
    chat_id = _chat_id_subquery(chat_telegram_id)
    user_id = _user_id_subquery(user_telegram_id) if user_telegram_id else None

    stmt = insert(Message).values(
        chat_id=chat_id,
        user_id=user_id,
        telegram_message_id=telegram_message_id,
        thread_id=thread_id,
        direction=direction,
        content_type=content_type,
        text=text,
        media_group_id=media_group_id,
        attachment_type=attachment_type,
        attachment_file_id=attachment_file_id,
        reply_to_message_id=reply_to_message_id,
        telegram_date=telegram_date,
        edited_at=edited_at,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_messages_chat_message",
        set_={
            "direction": stmt.excluded.direction,
            "content_type": stmt.excluded.content_type,
            "text": stmt.excluded.text,
            "media_group_id": stmt.excluded.media_group_id,
            "attachment_type": stmt.excluded.attachment_type,
            "attachment_file_id": stmt.excluded.attachment_file_id,
            "reply_to_message_id": stmt.excluded.reply_to_message_id,
            "thread_id": stmt.excluded.thread_id,
            "edited_at": stmt.excluded.edited_at,
            "updated_at": datetime.now(UTC),
        },
    ).returning(Message)

    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def mark_message_deleted(
    session: AsyncSession,
    *,
    chat_telegram_id: int,
    telegram_message_id: int,
    deleted_at: datetime | None = None,
) -> None:
    """Mark a message as deleted by setting deleted_at timestamp."""
    deleted_at = deleted_at or datetime.now(UTC)

    # Use subquery join to avoid extra round-trip
    stmt = (
        update(Message)
        .where(
            Message.chat_id.in_(
                select(Chat.id).where(Chat.telegram_id == chat_telegram_id)
            ),
            Message.telegram_message_id == telegram_message_id,
        )
        .values(deleted_at=deleted_at, updated_at=datetime.now(UTC))
    )
    await session.execute(stmt)


async def get_recent_messages(
    session: AsyncSession,
    chat_telegram_id: int,
    limit: int = 100,
) -> list[Message]:
    """Get recent non-deleted messages for a chat.

    Returns messages in chronological order (oldest first) for building
    LLM context. Joins directly on telegram_id to avoid N+1 queries.
    """
    with logfire.span(
        "db.get_recent_messages",
        **{
            "db.operation": "select",
            "telegram.chat_id": chat_telegram_id,
            "db.limit": limit,
        },
    ) as span:
        # Single query joining Chat and Message to avoid extra round-trip
        stmt = (
            select(Message)
            .join(Chat, Message.chat_id == Chat.id)
            .where(Chat.telegram_id == chat_telegram_id, Message.deleted_at.is_(None))
            .order_by(Message.created_at.desc(), Message.telegram_message_id.desc())
            .limit(limit)
            .options(selectinload(Message.user))
        )

        result = await session.execute(stmt)
        messages = list(result.scalars().all())

        # Reverse to get chronological order (oldest first)
        messages = list(reversed(messages))
        span.set_attribute("db.rows_returned", len(messages))
        return messages
