"""Where a tool's picture or clip comes from: a stored message, a member's profile photo or the chat photo.

Message media is the one named, or else the current message or the one it
replies to. A profile photo belongs to a stored message's sender, else the
person replied to, else the asker; tools never take a user id. Failures raise
``Refused``, whose text is meant for the model.
"""

from dataclasses import dataclass

from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.history.models import Message, MessageMedia
from derp.telegram.media import MediaUnavailableError, chat_photo_bytes, profile_photo_bytes, sniff_image

VISUAL = ("photo", "sticker", "document", "animation", "video", "video_note")
SOUND = ("voice", "audio", "video", "video_note")


class Refused(Exception):  # noqa: N818 - a message for the model, not an error
    pass


@dataclass(frozen=True, slots=True)
class Source:
    data: bytes
    type: str  # the Telegram media type it came from ("profile_photo" / "chat_photo" for avatars)
    image: bool  # a still picture Pillow can open (GIFs count as clips)


async def replied_to(deps: RunDeps) -> int | None:
    async with deps.db.session() as session:
        return await session.scalar(
            select(Message.reply_to_message_id).where(
                Message.chat_id == deps.chat.id, Message.message_id == deps.trigger_message_id
            )
        )


async def message_media(deps: RunDeps, message_id: int | None, types: tuple[str, ...]) -> Source:
    anchors = [message_id] if message_id is not None else [deps.trigger_message_id, await replied_to(deps)]
    async with deps.db.session() as session:
        rows = list(
            await session.scalars(
                select(MessageMedia)
                .join(
                    Message,
                    (Message.chat_id == MessageMedia.chat_id)
                    & (Message.message_id == MessageMedia.message_id),
                )
                .where(
                    MessageMedia.chat_id == deps.chat.id,
                    MessageMedia.message_id.in_([a for a in anchors if a]),
                    MessageMedia.type.in_(types),
                    Message.deleted_at.is_(None),
                )
                .order_by(MessageMedia.idx)
            )
        )
    rows.sort(key=lambda r: anchors.index(r.message_id))
    if not rows:
        where = (
            f"message #{message_id}" if message_id is not None else "this message or the one it replies to"
        )
        raise Refused(f"No usable media in {where}; name the message with message_id.")
    row = rows[0]
    try:
        data = await deps.gateway.fetch(
            row.file_id, bot_id=row.bot_id, key=row.file_unique_id, size_hint=row.file_size
        )
    except MediaUnavailableError as unavailable:
        raise Refused(f"That file can't be downloaded ({unavailable.reason}).") from None
    kind = sniff_image(data)
    return Source(data, row.type, image=kind is not None and kind != "image/gif")


async def photo_owner(deps: RunDeps, message_id: int | None) -> int:
    """Whose profile photo: message_id's sender, else the person replied to, else the asker."""
    anchor = message_id if message_id is not None else await replied_to(deps)
    if anchor is None:
        return deps.user_id
    async with deps.db.session() as session:
        sender = await session.scalar(
            select(Message.user_id).where(Message.chat_id == deps.chat.id, Message.message_id == anchor)
        )
    if sender is None and message_id is not None:
        raise Refused(f"Message #{message_id} isn't in this chat's history or has no person behind it.")
    return sender or deps.user_id


async def avatar_source(deps: RunDeps, message_id: int | None) -> Source:
    try:
        data = await profile_photo_bytes(deps.bot, deps.gateway, await photo_owner(deps, message_id))
    except MediaUnavailableError as unavailable:
        raise Refused(f"No profile photo to use: {unavailable.reason}.") from None
    return Source(data, "profile_photo", image=sniff_image(data) is not None)


async def chat_photo_source(deps: RunDeps) -> Source:
    try:
        data = await chat_photo_bytes(deps.bot, deps.gateway, deps.chat.id)
    except MediaUnavailableError as unavailable:
        raise Refused(f"No chat photo to use: {unavailable.reason}.") from None
    return Source(data, "chat_photo", image=sniff_image(data) is not None)
