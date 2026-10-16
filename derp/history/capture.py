"""Turn Telegram messages into history rows (users, chats, messages, media, topics).

Everything is an upsert keyed by Telegram IDs, so redelivered updates and
edits are idempotent. Ephemeral messages (``message_id == 0``) and guest-mode
messages are never stored.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from aiogram.types import Chat as TgChat
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.digests import mark_stale
from derp.history.models import Chat, ForumTopic, Message, MessageMedia, Sticker, User
from derp.history.polls import upsert_poll

TYPED_KINDS = {
    "text", "photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation",
    "poll", "dice", "location", "venue", "contact", "checklist", "story", "game", "invoice",
    "giveaway", "paid_media",
}  # fmt: skip
PAYLOAD_FIELDS = {
    "dice": "dice", "location": "location", "venue": "venue", "contact": "contact", "checklist": "checklist",
    "story": "story", "game": "game", "invoice": "invoice", "giveaway": "giveaway", "poll": "poll",
}  # fmt: skip
SERVICE_TYPES = {
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created", "message_auto_delete_timer_changed",
    "migrate_to_chat_id", "migrate_from_chat_id", "pinned_message", "forum_topic_created", "forum_topic_edited",
    "forum_topic_closed", "forum_topic_reopened", "general_forum_topic_hidden", "general_forum_topic_unhidden",
    "video_chat_scheduled", "video_chat_started", "video_chat_ended", "video_chat_participants_invited",
    "boost_added", "chat_background_set", "write_access_allowed", "users_shared", "chat_shared",
    "giveaway_created", "giveaway_winners", "giveaway_completed", "chat_owner_left", "chat_owner_changed",
    "proximity_alert_triggered", "successful_payment", "refunded_payment", "gift", "unique_gift",
    "checklist_tasks_done", "checklist_tasks_added", "direct_message_price_changed", "paid_message_price_changed",
}  # fmt: skip
STALE_NOTES = ("transcript", "transcript_model", "description", "description_run_id")
RAW_EXCLUDE = {"chat", "from_user", "reply_to_message", "text", "caption", "entities", "caption_entities"}


def utc(value: datetime | int) -> datetime:
    if isinstance(value, int):  # aiogram leaves some timestamps (edit_date) as Unix seconds
        return datetime.fromtimestamp(value, UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def content_type(message: TgMessage) -> str:
    content = message.content_type
    return str(getattr(content, "value", content))


def classify(message: TgMessage) -> str:
    content = content_type(message)
    if content in TYPED_KINDS:
        return content
    if content in {"giveaway_winners"} or content in SERVICE_TYPES:
        return "service"
    return "other"


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_dump(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def payload_for(message: TgMessage, kind: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if field := PAYLOAD_FIELDS.get(kind):
        payload = _dump(getattr(message, field))
        if kind == "poll" and message.poll is not None:
            payload = {
                "poll_id": message.poll.id,
                "question": message.poll.question,
                "options": [option.text for option in message.poll.options],
                "type": message.poll.type,
                "is_anonymous": message.poll.is_anonymous,
                "allows_multiple_answers": message.poll.allows_multiple_answers,
            }
    elif kind == "service":
        event = content_type(message)
        payload = {"event": event, "data": _service_data(message, event)}
    if message.external_reply is not None:
        payload["external_reply"] = _dump(message.external_reply)
    if message.story is not None and kind != "story":
        payload["story"] = _dump(message.story)
    return payload


def _service_data(message: TgMessage, event: str) -> Any:
    value = getattr(message, event, None)
    if event == "pinned_message" and message.pinned_message is not None:
        return {"message_id": message.pinned_message.message_id}
    if event == "new_chat_members" and message.new_chat_members:
        return [{"id": user.id, "name": user.full_name} for user in message.new_chat_members]
    if event == "left_chat_member" and message.left_chat_member is not None:
        return {"id": message.left_chat_member.id, "name": message.left_chat_member.full_name}
    if event == "new_chat_photo":
        return True
    return _dump(value)


def media_rows(message: TgMessage) -> list[dict[str, Any]]:
    """Media attached to a message; a photo keeps only its largest size."""
    rows: list[dict[str, Any]] = []
    base = {"chat_id": message.chat.id, "message_id": message.message_id}
    if message.photo:
        photo = max(message.photo, key=lambda size: size.width * size.height)
        rows.append(
            base | {"type": "photo", "file_id": photo.file_id, "file_unique_id": photo.file_unique_id,
                    "file_size": photo.file_size, "width": photo.width, "height": photo.height}
        )  # fmt: skip
    for kind in ("video", "voice", "video_note", "audio", "document", "sticker", "animation"):
        item = getattr(message, kind)
        if item is None:
            continue
        row: dict[str, Any] = base | {
            "type": kind,
            "file_id": item.file_id,
            "file_unique_id": item.file_unique_id,
            "file_size": getattr(item, "file_size", None),
            "mime_type": getattr(item, "mime_type", None),
            "duration": getattr(item, "duration", None),
            "file_name": getattr(item, "file_name", None),
        }
        width = getattr(item, "width", None) or getattr(item, "length", None)
        height = getattr(item, "height", None) or getattr(item, "length", None)
        row |= {"width": width, "height": height}
        if kind == "sticker":
            row |= {"sticker_emoji": item.emoji, "sticker_set": item.set_name}
        rows.append(row)
        break
    for idx, row in enumerate(rows):
        row["idx"] = idx
    return rows


async def upsert_user(session: AsyncSession, user: TgUser) -> None:
    values = {
        "id": user.id,
        "is_bot": user.is_bot,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
        "language_code": user.language_code,
        "is_premium": bool(user.is_premium),
    }
    statement = insert(User).values(values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[User.id],
            set_={key: statement.excluded[key] for key in values if key != "id"} | {"updated_at": func.now()},
        )
    )


async def upsert_chat(session: AsyncSession, chat: TgChat) -> Chat:
    values = {
        "id": chat.id,
        "type": chat.type,
        "title": chat.title or chat.full_name if chat.type != "private" else None,
        "username": chat.username,
        "is_forum": bool(chat.is_forum),
    }
    statement = insert(Chat).values(values)
    statement = statement.on_conflict_do_update(
        index_elements=[Chat.id],
        set_={key: statement.excluded[key] for key in values if key != "id"} | {"updated_at": func.now()},
    ).returning(Chat)
    result = await session.scalars(statement, execution_options={"populate_existing": True})
    return result.one()


def _people(message: TgMessage) -> Iterable[TgUser]:
    if message.from_user is not None:
        yield message.from_user
    yield from message.new_chat_members or []
    if message.left_chat_member is not None:
        yield message.left_chat_member


async def store_message(session: AsyncSession, message: TgMessage, *, capture: str) -> bool:
    """Insert or update (on edit) one message and its media. Returns False when not storable."""
    if message.message_id <= 0 or message.guest_query_id:
        return False
    for person in _people(message):
        await upsert_user(session, person)
    kind = classify(message)
    reply = message.reply_to_message
    values: dict[str, Any] = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "thread_id": message.message_thread_id if message.is_topic_message else None,
        "user_id": message.from_user.id if message.from_user else None,
        "sender_chat_id": message.sender_chat.id if message.sender_chat else None,
        "date": utc(message.date),
        "edit_date": utc(message.edit_date) if message.edit_date else None,
        "kind": kind,
        "text": message.text or message.caption,
        "entities": _dump(message.entities or message.caption_entities) or None,
        "reply_to_message_id": reply.message_id
        if reply is not None and reply.chat.id == message.chat.id
        else None,
        "quote": message.quote.text if message.quote else None,
        "forward_origin": _dump(message.forward_origin) if message.forward_origin else None,
        "media_group_id": message.media_group_id,
        "via_bot_id": message.via_bot.id if message.via_bot else None,
        "capture": capture,
        "is_derp": capture == "derp",
        "payload": payload_for(message, kind),
        "raw": message.model_dump(mode="json", exclude_none=True, exclude=RAW_EXCLUDE)
        if kind == "other"
        else None,
    }
    statement = insert(Message).values(values)
    updatable = ("edit_date", "text", "entities", "payload", "raw", "kind")
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Message.chat_id, Message.message_id],
            set_={key: statement.excluded[key] for key in updatable},
        )
    )
    media = media_rows(message)
    if media:
        existing = {
            r[0]: r[1]
            for r in await session.execute(
                select(MessageMedia.idx, MessageMedia.file_unique_id).where(
                    MessageMedia.chat_id == message.chat.id, MessageMedia.message_id == message.message_id
                )
            )
        }
        replaced = {
            r["idx"] for r in media if existing.get(r["idx"], r["file_unique_id"]) != r["file_unique_id"]
        }
        if replaced:  # an edit swapped the file: notes about the old one no longer apply
            await session.execute(
                update(Message)
                .where(Message.chat_id == message.chat.id, Message.message_id == message.message_id)
                .values(extra_text=None)
            )
        await session.execute(
            delete(MessageMedia).where(
                MessageMedia.chat_id == message.chat.id,
                MessageMedia.message_id == message.message_id,
                MessageMedia.idx >= len(media),
            )
        )
        for row in media:
            media_statement = insert(MessageMedia).values(row)
            await session.execute(
                media_statement.on_conflict_do_update(
                    index_elements=[MessageMedia.chat_id, MessageMedia.message_id, MessageMedia.idx],
                    set_={
                        k: media_statement.excluded[k]
                        for k in row
                        if k not in {"chat_id", "message_id", "idx"}
                    }
                    | (dict.fromkeys(STALE_NOTES) if row["idx"] in replaced else {}),
                )
            )
    if message.poll is not None:
        await upsert_poll(
            session,
            message.poll,
            chat_id=message.chat.id,
            message_id=message.message_id,
            by_derp=capture == "derp",
        )
    if message.edit_date:  # summaries of the day no longer match what was said
        await mark_stale(session, message.chat.id, message.message_id)
    await _track_topic(session, message)
    return True


async def _track_topic(session: AsyncSession, message: TgMessage) -> None:
    thread = message.message_thread_id
    if thread is None:
        return
    if created := message.forum_topic_created:
        statement = insert(ForumTopic).values(
            chat_id=message.chat.id,
            thread_id=thread,
            name=created.name,
            icon_custom_emoji_id=created.icon_custom_emoji_id,
        )
        await session.execute(statement.on_conflict_do_nothing())
        return
    topic = await session.get(ForumTopic, (message.chat.id, thread))
    if topic is None:
        return
    if edited := message.forum_topic_edited:
        topic.name = edited.name or topic.name
        topic.icon_custom_emoji_id = edited.icon_custom_emoji_id or topic.icon_custom_emoji_id
    elif message.forum_topic_closed is not None:
        topic.closed = True
    elif message.forum_topic_reopened is not None:
        topic.closed = False


async def index_sticker(session: AsyncSession, message: TgMessage) -> None:
    """Remember every sticker seen (stored message or not): ``send_sticker`` picks from these."""
    sticker = message.sticker
    if sticker is None or sticker.type == "custom_emoji":
        return
    statement = insert(Sticker).values(
        file_unique_id=sticker.file_unique_id,
        file_id=sticker.file_id,
        emoji=sticker.emoji,
        set_name=sticker.set_name,
        kind=sticker.type,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Sticker.file_unique_id],
            set_={"file_id": statement.excluded.file_id, "uses": Sticker.uses + 1},
        )
    )


async def is_stored(session: AsyncSession, chat_id: int, message_id: int) -> bool:
    found = await session.scalar(
        select(Message.message_id).where(Message.chat_id == chat_id, Message.message_id == message_id)
    )
    return found is not None


async def record_outgoing(session: AsyncSession, messages: Iterable[TgMessage]) -> None:
    """Derp's own messages, recorded after Telegram accepted them."""
    for message in messages:
        await store_message(session, message, capture="derp")
