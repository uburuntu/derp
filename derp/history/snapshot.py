"""Pure projection of aiogram messages into normalized source snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from aiogram.types import (
    Animation,
    Audio,
    Document,
    LivePhoto,
    Message,
    MessageEntity,
    PaidMediaLivePhoto,
    PaidMediaPhoto,
    PaidMediaVideo,
    PhotoSize,
    Sticker,
    Video,
    VideoNote,
    Voice,
)

TELEGRAM_SNAPSHOT_SCHEMA_VERSION: Final = 1


class SnapshotRole(StrEnum):
    """Conversation role assigned to a captured Telegram message."""

    USER = "user"
    ASSISTANT = "assistant"


class MessageDirection(StrEnum):
    """Direction of a message relative to Derp."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CaptureKind(StrEnum):
    """Reason the source message was retained."""

    AMBIENT = "ambient"
    EXPLICIT = "explicit"


class SenderKind(StrEnum):
    """Telegram principal represented by a sender snapshot."""

    USER = "user"
    CHAT = "chat"


class TopicKind(StrEnum):
    """Telegram scope containing the message."""

    CHAT = "chat"
    THREAD = "thread"
    DIRECT_MESSAGES = "direct_messages"


class ReplyKind(StrEnum):
    """Kind of reply target without copying the target payload."""

    NONE = "none"
    MESSAGE = "message"
    EXTERNAL_MESSAGE = "external_message"
    STORY = "story"


class EditKind(StrEnum):
    """Whether Telegram reports a message as edited."""

    ORIGINAL = "original"
    EDITED = "edited"


class MediaGroupKind(StrEnum):
    """Whether a message belongs to a Telegram album."""

    NONE = "none"
    GROUPED = "grouped"


class AttachmentMediaType(StrEnum):
    """Supported stable Telegram attachment types."""

    ANIMATION = "animation"
    AUDIO = "audio"
    DOCUMENT = "document"
    LIVE_PHOTO = "live_photo"
    PHOTO = "photo"
    STICKER = "sticker"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class SenderSnapshot:
    """Allowlisted Telegram sender identity."""

    kind: SenderKind
    id: int
    is_bot: bool | None = None
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    title: str | None = None
    chat_type: str | None = None


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """Allowlisted formatting metadata for text or caption content."""

    type: str
    offset: int
    length: int
    url: str | None = None
    user_id: int | None = None
    language: str | None = None
    custom_emoji_id: str | None = None
    unix_time: int | None = None
    date_time_format: str | None = None


@dataclass(frozen=True, slots=True)
class TopicSnapshot:
    """Stable Telegram topic classification."""

    kind: TopicKind
    topic_id: int | None = None


@dataclass(frozen=True, slots=True)
class ReplySnapshot:
    """Stable reply coordinates without a nested message body."""

    kind: ReplyKind
    chat_id: int | None = None
    message_id: int | None = None
    story_id: int | None = None


@dataclass(frozen=True, slots=True)
class EditSnapshot:
    """Telegram edit state and timestamp."""

    kind: EditKind
    edited_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MediaGroupSnapshot:
    """Telegram album membership."""

    kind: MediaGroupKind
    media_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentSnapshot:
    """Stable reference and scalar metadata for one Telegram attachment."""

    media_type: AttachmentMediaType
    file_id: str
    file_unique_id: str
    mime_type: str | None = None
    filename: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramMessageSnapshot:
    """Versioned, normalized, conversational subset of a Telegram message."""

    role: SnapshotRole
    direction: MessageDirection
    capture: CaptureKind
    chat_id: int
    message_id: int
    sent_at: datetime
    content_type: str
    topic: TopicSnapshot
    reply: ReplySnapshot
    edit: EditSnapshot
    media_group: MediaGroupSnapshot
    sender: SenderSnapshot | None = None
    text: str | None = None
    caption: str | None = None
    entities: tuple[EntitySnapshot, ...] = ()
    caption_entities: tuple[EntitySnapshot, ...] = ()
    attachments: tuple[AttachmentSnapshot, ...] = ()
    schema_version: int = field(
        default=TELEGRAM_SNAPSHOT_SCHEMA_VERSION,
        init=False,
    )


def project_message_snapshot(
    message: Message,
    *,
    role: SnapshotRole,
    direction: MessageDirection,
    capture: CaptureKind,
) -> TelegramMessageSnapshot:
    """Project a Telegram message without I/O or unrestricted object dumping."""
    sent_at = _as_utc_datetime(message.date)
    if sent_at is None:
        raise ValueError("Telegram messages require a sent timestamp")

    return TelegramMessageSnapshot(
        role=role,
        direction=direction,
        capture=capture,
        chat_id=message.chat.id,
        message_id=message.message_id,
        sent_at=sent_at,
        content_type=message.content_type,
        topic=_project_topic(message),
        reply=_project_reply(message),
        edit=_project_edit(message),
        media_group=_project_media_group(message),
        sender=_project_sender(message),
        text=message.text,
        caption=message.caption,
        entities=_project_entities(message.entities),
        caption_entities=_project_entities(message.caption_entities),
        attachments=_project_attachments(message),
    )


def _as_utc_datetime(value: datetime | int | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value, UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _project_sender(message: Message) -> SenderSnapshot | None:
    if sender_chat := message.sender_chat:
        return SenderSnapshot(
            kind=SenderKind.CHAT,
            id=sender_chat.id,
            username=sender_chat.username,
            title=sender_chat.title,
            chat_type=sender_chat.type,
        )
    if sender := message.from_user:
        return SenderSnapshot(
            kind=SenderKind.USER,
            id=sender.id,
            is_bot=sender.is_bot,
            first_name=sender.first_name,
            last_name=sender.last_name,
            username=sender.username,
        )
    return None


def _project_entities(
    entities: list[MessageEntity] | None,
) -> tuple[EntitySnapshot, ...]:
    return tuple(
        EntitySnapshot(
            type=entity.type,
            offset=entity.offset,
            length=entity.length,
            url=entity.url,
            user_id=entity.user.id if entity.user else None,
            language=entity.language,
            custom_emoji_id=entity.custom_emoji_id,
            unix_time=entity.unix_time,
            date_time_format=entity.date_time_format,
        )
        for entity in entities or ()
    )


def _project_topic(message: Message) -> TopicSnapshot:
    if topic := message.direct_messages_topic:
        return TopicSnapshot(TopicKind.DIRECT_MESSAGES, topic.topic_id)
    if message.is_topic_message or message.message_thread_id is not None:
        return TopicSnapshot(TopicKind.THREAD, message.message_thread_id)
    return TopicSnapshot(TopicKind.CHAT)


def _project_reply(message: Message) -> ReplySnapshot:
    if reply := message.reply_to_message:
        return ReplySnapshot(ReplyKind.MESSAGE, message_id=reply.message_id)
    if reply := message.external_reply:
        return ReplySnapshot(
            ReplyKind.EXTERNAL_MESSAGE,
            chat_id=reply.chat.id if reply.chat else None,
            message_id=reply.message_id,
        )
    if story := message.reply_to_story:
        return ReplySnapshot(
            ReplyKind.STORY,
            chat_id=story.chat.id,
            story_id=story.id,
        )
    return ReplySnapshot(ReplyKind.NONE)


def _project_edit(message: Message) -> EditSnapshot:
    edited_at = _as_utc_datetime(message.edit_date)
    return EditSnapshot(
        EditKind.EDITED if edited_at else EditKind.ORIGINAL,
        edited_at=edited_at,
    )


def _project_media_group(message: Message) -> MediaGroupSnapshot:
    if media_group_id := message.media_group_id:
        return MediaGroupSnapshot(MediaGroupKind.GROUPED, media_group_id)
    return MediaGroupSnapshot(MediaGroupKind.NONE)


def _attachment(
    media_type: AttachmentMediaType,
    file_id: str,
    file_unique_id: str,
    *,
    mime_type: str | None = None,
    filename: str | None = None,
    size: int | None = None,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
) -> AttachmentSnapshot:
    return AttachmentSnapshot(
        media_type=media_type,
        file_id=file_id,
        file_unique_id=file_unique_id,
        mime_type=mime_type,
        filename=filename,
        size=size,
        width=width,
        height=height,
        duration=duration,
    )


def _photo_attachment(photos: list[PhotoSize]) -> AttachmentSnapshot | None:
    if not photos:
        return None
    photo = max(
        photos,
        key=lambda item: (item.width * item.height, item.file_size or 0),
    )
    return _attachment(
        AttachmentMediaType.PHOTO,
        photo.file_id,
        photo.file_unique_id,
        size=photo.file_size,
        width=photo.width,
        height=photo.height,
    )


def _animation_attachment(animation: Animation) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.ANIMATION,
        animation.file_id,
        animation.file_unique_id,
        mime_type=animation.mime_type,
        filename=animation.file_name,
        size=animation.file_size,
        width=animation.width,
        height=animation.height,
        duration=animation.duration,
    )


def _audio_attachment(audio: Audio) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.AUDIO,
        audio.file_id,
        audio.file_unique_id,
        mime_type=audio.mime_type,
        filename=audio.file_name,
        size=audio.file_size,
        duration=audio.duration,
    )


def _document_attachment(document: Document) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.DOCUMENT,
        document.file_id,
        document.file_unique_id,
        mime_type=document.mime_type,
        filename=document.file_name,
        size=document.file_size,
    )


def _live_photo_attachments(
    live_photo: LivePhoto,
    fallback_photo: list[PhotoSize] | None = None,
) -> tuple[AttachmentSnapshot, ...]:
    attachments = [
        _attachment(
            AttachmentMediaType.LIVE_PHOTO,
            live_photo.file_id,
            live_photo.file_unique_id,
            mime_type=live_photo.mime_type,
            size=live_photo.file_size,
            width=live_photo.width,
            height=live_photo.height,
            duration=live_photo.duration,
        )
    ]
    if photo := _photo_attachment(live_photo.photo or fallback_photo or []):
        attachments.append(photo)
    return tuple(attachments)


def _sticker_attachment(sticker: Sticker) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.STICKER,
        sticker.file_id,
        sticker.file_unique_id,
        size=sticker.file_size,
        width=sticker.width,
        height=sticker.height,
    )


def _video_attachment(video: Video) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.VIDEO,
        video.file_id,
        video.file_unique_id,
        mime_type=video.mime_type,
        filename=video.file_name,
        size=video.file_size,
        width=video.width,
        height=video.height,
        duration=video.duration,
    )


def _video_note_attachment(video_note: VideoNote) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.VIDEO_NOTE,
        video_note.file_id,
        video_note.file_unique_id,
        size=video_note.file_size,
        width=video_note.length,
        height=video_note.length,
        duration=video_note.duration,
    )


def _voice_attachment(voice: Voice) -> AttachmentSnapshot:
    return _attachment(
        AttachmentMediaType.VOICE,
        voice.file_id,
        voice.file_unique_id,
        mime_type=voice.mime_type,
        size=voice.file_size,
        duration=voice.duration,
    )


def _project_paid_media(message: Message) -> tuple[AttachmentSnapshot, ...]:
    if not message.paid_media:
        return ()

    attachments: list[AttachmentSnapshot] = []
    for media in message.paid_media.paid_media:
        if isinstance(media, PaidMediaPhoto):
            if photo := _photo_attachment(media.photo):
                attachments.append(photo)
        elif isinstance(media, PaidMediaVideo):
            attachments.append(_video_attachment(media.video))
        elif isinstance(media, PaidMediaLivePhoto):
            attachments.extend(_live_photo_attachments(media.live_photo))
    return tuple(attachments)


def _project_attachments(message: Message) -> tuple[AttachmentSnapshot, ...]:
    if message.paid_media:
        return _project_paid_media(message)
    if live_photo := message.live_photo:
        return _live_photo_attachments(live_photo, message.photo)
    if animation := message.animation:
        return (_animation_attachment(animation),)
    if audio := message.audio:
        return (_audio_attachment(audio),)
    if document := message.document:
        return (_document_attachment(document),)
    if photo := message.photo:
        attachment = _photo_attachment(photo)
        return (attachment,) if attachment else ()
    if sticker := message.sticker:
        return (_sticker_attachment(sticker),)
    if video := message.video:
        return (_video_attachment(video),)
    if video_note := message.video_note:
        return (_video_note_attachment(video_note),)
    if voice := message.voice:
        return (_voice_attachment(voice),)
    return ()


__all__ = [
    "AttachmentMediaType",
    "AttachmentSnapshot",
    "CaptureKind",
    "EditKind",
    "EditSnapshot",
    "EntitySnapshot",
    "MediaGroupKind",
    "MediaGroupSnapshot",
    "MessageDirection",
    "ReplyKind",
    "ReplySnapshot",
    "SenderKind",
    "SenderSnapshot",
    "SnapshotRole",
    "TELEGRAM_SNAPSHOT_SCHEMA_VERSION",
    "TelegramMessageSnapshot",
    "TopicKind",
    "TopicSnapshot",
    "project_message_snapshot",
]
