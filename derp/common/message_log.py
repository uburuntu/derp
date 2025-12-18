"""Helpers to project Telegram updates/messages into the messages table.

This module performs upserts into the `messages` table using SQLAlchemy,
building cleaned records for LLM context.
"""

from __future__ import annotations

import datetime as _dt

from aiogram.types import Message, Update

from derp.common.tg import extract_attachment_info
from derp.db import DatabaseManager, mark_message_deleted, upsert_message


async def upsert_message_from_update(
    db: DatabaseManager,
    *,
    update: Update,
    direction: str = "in",
) -> None:
    """Project supported update kinds into messages table via upsert.

    Supported: message, edited_message, channel_post, edited_channel_post.
    """
    msg: Message | None = None
    if update.message:
        msg = update.message
    elif update.edited_message:
        msg = update.edited_message
    elif update.channel_post:
        msg = update.channel_post
    elif update.edited_channel_post:
        msg = update.edited_channel_post

    if not msg:
        return

    await upsert_message_from_message(
        db,
        message=msg,
        direction=direction,
    )


async def upsert_message_from_message(
    db: DatabaseManager,
    *,
    message: Message,
    direction: str,
) -> None:
    """Upsert a message record from a Telegram Message object."""
    attachment_type, attachment_file_id, _ = extract_attachment_info(message)

    # Extract edited_at from edit_date
    edited_at = None
    if message.edit_date:
        if isinstance(message.edit_date, int):
            edited_at = _dt.datetime.fromtimestamp(message.edit_date, _dt.UTC)
        else:
            edited_at = message.edit_date

    async with db.session() as session:
        await upsert_message(
            session,
            chat_telegram_id=message.chat.id,
            user_telegram_id=message.from_user.id if message.from_user else None,
            telegram_message_id=message.message_id,
            thread_id=message.message_thread_id,
            direction=direction,
            content_type=message.content_type,
            text=message.html_text,
            media_group_id=message.media_group_id,
            attachment_type=attachment_type,
            attachment_file_id=attachment_file_id,
            reply_to_message_id=(
                message.reply_to_message.message_id
                if message.reply_to_message
                else None
            ),
            telegram_date=message.date,
            edited_at=edited_at,
        )


async def mark_deleted(
    db: DatabaseManager,
    *,
    chat_id: int,
    message_id: int,
    when: _dt.datetime | None = None,
) -> None:
    """Mark a message as deleted in the messages table."""
    when = when or _dt.datetime.now(tz=_dt.UTC)
    async with db.session() as session:
        await mark_message_deleted(
            session,
            chat_telegram_id=chat_id,
            telegram_message_id=message_id,
            deleted_at=when,
        )
