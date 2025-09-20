"""Helpers to project Telegram updates/messages into the cleaned MessageLog.

This module performs application-managed upserts into `telegram::MessageLog`
using a natural key `message_key` ("{chat_id}:{thread_id or 0}:{message_id}").
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import gel
from aiogram.types import Message, Update

from ..queries.mark_message_log_deleted_async_edgeql import mark_message_log_deleted
from ..queries.upsert_message_log_async_edgeql import upsert_message_log
from .tg import extract_attachment_info


def _key(chat_id: int, message_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id or 0}:{message_id}"


def _extract_core(msg: Message) -> dict[str, Any]:
    """Extract a normalized payload from aiogram Message."""
    attachment_type, attachment_file_id, _ = extract_attachment_info(msg)

    return {
        "message_id": msg.message_id,
        "thread_id": msg.message_thread_id,
        "chat_id": msg.chat.id,
        "from_user_id": msg.from_user and msg.from_user.id,
        "content_type": msg.content_type,
        "text": msg.html_text,
        "media_group_id": msg.media_group_id,
        "attachment_type": attachment_type,
        "attachment_file_id": attachment_file_id,
        "reply_to_message_id": msg.reply_to_message and msg.reply_to_message.message_id,
        "tg_date": msg.date,
        "edited_at": msg.edit_date
        and _dt.datetime.fromtimestamp(msg.edit_date, _dt.UTC)
        or msg.date,
    }


async def upsert_message_from_update(
    executor: gel.AsyncIOExecutor,
    *,
    update: Update,
    direction: str = "in",
) -> None:
    """Project supported update kinds into MessageLog via upsert.

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
        executor,
        message=msg,
        direction=direction,
        source_update_id_by_update_id=update.update_id,
    )


async def upsert_message_from_message(
    executor: gel.AsyncIOExecutor,
    *,
    message: Message,
    direction: str,
    source_update_id_by_update_id: int | None = None,
) -> None:
    """Upsert MessageLog from a Telegram Message object.

    If `source_update_id_by_update_id` is provided, the upsert will try to
    resolve the `telegram::BotUpdate` with that `update_id` and link it as
    `source_update` (best-effort).
    """
    p = _extract_core(message)
    message_key = _key(p["chat_id"], p["message_id"], p["thread_id"])

    await upsert_message_log(
        executor,
        chat_id=p["chat_id"],
        from_user_id=p["from_user_id"],
        message_key=message_key,
        direction=direction,
        message_id=p["message_id"],
        thread_id=p["thread_id"],
        content_type=p["content_type"],
        text=p["text"],
        media_group_id=p["media_group_id"],
        attachment_type=p["attachment_type"],
        attachment_file_id=p["attachment_file_id"],
        reply_to_message_id=p["reply_to_message_id"],
        tg_date=p["tg_date"],
        edited_at=p["edited_at"],
        source_update_id=source_update_id_by_update_id,
    )


async def mark_deleted(
    executor: gel.AsyncIOExecutor,
    *,
    chat_id: int,
    message_id: int,
    thread_id: int | None,
    when: _dt.datetime | None = None,
) -> None:
    """Mark a message as deleted in the projection table."""
    when = when or _dt.datetime.now(tz=_dt.UTC)
    await mark_message_log_deleted(
        executor,
        message_key=_key(chat_id, message_id, thread_id),
        when=when,
    )
