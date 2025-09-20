import asyncio
from enum import IntEnum, auto

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Animation,
    Audio,
    Chat,
    Document,
    Message,
    PhotoSize,
    Sticker,
    TelegramObject,
    Update,
    User,
    Video,
    VideoNote,
    Voice,
)
from pydantic import BaseModel

from .utils import one_liner


def user_info(user: User, sender_chat: Chat | None = None) -> str:
    if sender_chat:
        return chat_info(sender_chat)

    last_name = " " + user.last_name if user.last_name else ""
    username = ", @" + user.username if user.username else ""
    language_code = ", " + user.language_code if user.language_code else ""
    return f"{user.first_name}{last_name} ({user.id}{username}{language_code})"


def chat_info(chat: Chat) -> str:
    if chat.type == "private":
        return "private"

    username = ", @" + chat.username if chat.username else ""
    return f"{chat.type} | {chat.title} ({chat.id}{username})"


def message_info(message: Message) -> str:
    prefix = f"{message.message_id} | "
    if message.text:
        return prefix + one_liner(message.text, cut_len=50)
    return prefix + f"type: {message.content_type}"


def decompose_update(
    update: Update,
) -> tuple[TelegramObject, User | None, Chat | None, Chat | None, str]:
    user, sender_chat, chat = None, None, None

    if f := update.message:
        user = f.from_user
        sender_chat = f.sender_chat
        chat = f.chat
        info = message_info(f)
    elif f := update.edited_message:
        user = f.from_user
        sender_chat = f.sender_chat
        chat = f.chat
        info = message_info(f) + " [edited]"
    elif f := update.channel_post:
        chat = f.chat
        info = message_info(f)
    elif f := update.edited_channel_post:
        chat = f.chat
        info = message_info(f) + " [edited]"
    elif f := update.inline_query:
        user = f.from_user
        info = one_liner(f.query, cut_len=50)
    elif f := update.chosen_inline_result:
        user = f.from_user
        info = one_liner(f.query, cut_len=50)
    elif f := update.callback_query:
        if f.message:
            chat = f.message.chat
        user = f.from_user
        info = f.data
    elif f := update.shipping_query:
        user = f.from_user
        info = f.as_json()
    elif f := update.pre_checkout_query:
        user = f.from_user
        info = f.as_json()
    elif f := update.poll:
        info = (
            f"{one_liner(f.question, cut_len=50)} ({f.id}),"
            f" {[o.text for o in f.options]}, {f.total_voter_count} voter(s)"
        )
    elif f := update.poll_answer:
        user = f.user
        info = f"{f.option_ids} ({f.poll_id})"
    elif f := (update.chat_member or update.my_chat_member):
        user = f.from_user
        chat = f.chat
        info = (
            f"{user_info(f.new_chat_member.user)}: {f.old_chat_member.status} ->"
            f" {f.new_chat_member.status}"
        )
    else:
        f = update
        info = update.as_json()

    return f, user, sender_chat, chat, info


async def profile_photo(u: User) -> PhotoSize | None:
    photos = await u.get_profile_photos(limit=1)
    if photos.total_count < 1:
        return None
    return photos.photos[0][-1]


async def create_sensitive_url_from_file_id(bot: Bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    return bot.session.api.file_url(bot.token, file.file_path)


def extract_attachment_info(
    message: Message,
) -> tuple[str | None, str | None, str | None]:
    attachment_type = None
    attachment_file_id = None
    attachment_filename = None

    if a := message.photo:
        attachment_type = "photo"
        attachment_file_id = a[-1].file_id
    elif a := message.audio:
        attachment_type = "audio"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name
    elif a := message.voice:
        attachment_type = "voice"
        attachment_file_id = a.file_id
    elif a := message.sticker:
        attachment_type = "sticker"
        attachment_file_id = a.file_id
    elif a := message.video:
        attachment_type = "video"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name
    elif a := message.video_note:
        attachment_type = "video_note"
        attachment_file_id = a.file_id
    elif a := message.animation:
        attachment_type = "animation"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name
    elif a := message.document:
        attachment_type = "document"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name

    return attachment_type, attachment_file_id, attachment_filename


async def extract_attachment_info_with_url(
    message: Message,
) -> tuple[str | None, str | None, str | None, str | None]:
    attachment_type, attachment_file_id, attachment_filename = extract_attachment_info(
        message
    )
    attachment_url = None

    if attachment_file_id:
        attachment_url = await create_sensitive_url_from_file_id(
            message.bot, attachment_file_id
        )

    return attachment_type, attachment_file_id, attachment_filename, attachment_url


def extract_attachment_file_id(message: Message) -> str | None:
    _, attachment_file_id, _ = extract_attachment_info(message)

    return attachment_file_id


async def reply_with_attachment(
    message: Message,
    text: str,
    attachment_type: str,
    attachment_file_id: str,
    attachment_url_fallback: str | None = None,
):
    async def send(method):
        try:
            return await method(attachment_file_id, caption=text)
        except TelegramBadRequest:
            if attachment_url_fallback:
                return await method(attachment_url_fallback, caption=text)
            raise

    match attachment_type:
        case "photo":
            return await send(message.reply_photo)
        case "audio":
            return await send(message.reply_audio)
        case "voice":
            return await send(message.reply_voice)
        case "sticker":
            return await send(message.reply_sticker)
        case "video":
            return await send(message.reply_video)
        case "video_note":
            return await send(message.reply_video_note)
        case "animation":
            return await send(message.reply_animation)
        case "document":
            return await send(message.reply_document)

    return await message.reply(text, disable_web_page_preview=False)


class ExtractedMedia(BaseModel):
    """Base class for extracted media content."""

    message: Message
    media: (
        PhotoSize | Document | Sticker | Video | Animation | VideoNote | Audio | Voice
    )

    model_config = {"arbitrary_types_allowed": True}

    @property
    def file_id(self) -> str:
        """Get the file ID of the media."""
        return self.media.file_id

    @property
    def file_size(self) -> int | None:
        """Get the file size if available."""
        return getattr(self.media, "file_size", None)

    @property
    def media_type(self) -> str:
        """Get the appropriate media type for this media object."""
        if isinstance(self.media, PhotoSize):
            return "image/jpeg"
        elif isinstance(self.media, Document):
            return self.media.mime_type or "application/octet-stream"
        elif isinstance(self.media, Sticker):
            if self.media.is_animated:
                # Lottie JSON animation
                return "application/json"
            elif self.media.is_video:
                return "video/webm"
            else:
                return "image/webp"
        elif isinstance(self.media, Video):
            return self.media.mime_type or "video/mp4"
        elif isinstance(self.media, Animation):
            return self.media.mime_type or "video/mp4"
        elif isinstance(self.media, VideoNote):
            return "video/mp4"
        elif isinstance(self.media, Audio):
            return self.media.mime_type or "audio/mpeg"
        elif isinstance(self.media, Voice):
            return self.media.mime_type or "audio/ogg"

        return "application/octet-stream"

    async def download(self) -> bytes:
        """Download the media and return raw bytes."""
        try:
            download_url = await create_sensitive_url_from_file_id(
                self.message.bot, self.file_id
            )
            async with httpx.AsyncClient() as client:
                response = await client.get(download_url)
                response.raise_for_status()
                return response.content
        except Exception as e:
            raise RuntimeError("Failed to download media") from e


class ExtractedPhoto(ExtractedMedia):
    """Extracted photo content."""

    media: PhotoSize | Document | Sticker

    @property
    def width(self) -> int | None:
        """Get photo width if available."""
        return getattr(self.media, "width", None)

    @property
    def height(self) -> int | None:
        """Get photo height if available."""
        return getattr(self.media, "height", None)


class ExtractedVideo(ExtractedMedia):
    """Extracted video content."""

    media: Video | Animation | VideoNote | Sticker

    @property
    def duration(self) -> int | None:
        """Get video duration if available."""
        return getattr(self.media, "duration", None)

    @property
    def width(self) -> int | None:
        """Get video width if available."""
        return getattr(self.media, "width", None)

    @property
    def height(self) -> int | None:
        """Get video height if available."""
        return getattr(self.media, "height", None)


class ExtractedAudio(ExtractedMedia):
    """Extracted audio content."""

    media: Audio | Voice

    @property
    def duration(self) -> int | None:
        """Get audio duration if available."""
        return getattr(self.media, "duration", None)

    @property
    def title(self) -> str | None:
        """Get audio title if available."""
        return getattr(self.media, "title", None)

    @property
    def performer(self) -> str | None:
        """Get audio performer if available."""
        return getattr(self.media, "performer", None)


class ExtractedDocument(ExtractedMedia):
    """Extracted document content."""

    media: Document

    @property
    def mime_type(self) -> str | None:
        """Get document MIME type."""
        return self.media.mime_type

    @property
    def file_name(self) -> str | None:
        """Get original filename."""
        return self.media.file_name


class ExtractedText(BaseModel):
    """Extracted text content."""

    message: Message
    text: str

    model_config = {"arbitrary_types_allowed": True}

    @property
    def length(self) -> int:
        """Get text length."""
        return len(self.text)

    def startswith(self, prefix: str) -> bool:
        """Check if text starts with prefix."""
        return self.text.startswith(prefix)

    def contains(self, substring: str) -> bool:
        """Check if text contains substring."""
        return substring in self.text


class Extractor:
    """Modern extractor for media and text from Telegram messages."""

    class ReplyPolicy(IntEnum):
        """Policy for checking reply messages."""

        only_origin = auto()
        prefer_origin = auto()
        prefer_reply = auto()
        only_reply = auto()

    @classmethod
    def _extract_photo_from_message(
        cls, message: Message
    ) -> PhotoSize | Document | Sticker | None:
        """Extract photo-like content from a single message."""
        if message.photo:
            return message.photo[-1]  # Get the largest photo

        if message.document:
            doc = message.document
            if doc.mime_type and doc.mime_type.startswith("image/"):
                supported_types = ("jpeg", "jpg", "png", "tiff", "bmp", "gif", "webp")
                if any(doc.mime_type.endswith(t) for t in supported_types):
                    return doc

        if (
            message.sticker
            and not message.sticker.is_animated
            and not message.sticker.is_video
        ):
            return message.sticker

        return None

    @classmethod
    async def _extract_profile_photo_from_message(
        cls, message: Message
    ) -> PhotoSize | Document | Sticker | None:
        """Extract profile photo from a single message."""
        if message.forward_from:
            if pp := await profile_photo(message.forward_from):
                return pp

        return await profile_photo(message.from_user)

    @classmethod
    def _extract_video_from_message(
        cls, message: Message
    ) -> Video | Animation | VideoNote | Sticker | None:
        """Extract video-like content from a single message."""
        # Only include video stickers, skip animated ones (they're SVG)
        if message.sticker and message.sticker.is_video:
            return message.sticker

        return message.video or message.animation or message.video_note

    @classmethod
    def _extract_audio_from_message(cls, message: Message) -> Audio | Voice | None:
        """Extract audio content from a single message."""
        return message.audio or message.voice

    @classmethod
    def _extract_document_from_message(cls, message: Message) -> Document | None:
        """Extract document from a single message."""
        return message.document

    @classmethod
    def _extract_text_from_message(cls, message: Message) -> str | None:
        """Extract text content from a single message."""
        return message.text or message.caption

    @classmethod
    async def _extract_with_policy(
        cls,
        message: Message,
        extractor_func,
        reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin,
    ):
        """Extract content using the specified reply policy."""

        async def call_extractor(msg):
            if asyncio.iscoroutinefunction(extractor_func):
                return await extractor_func(msg)
            return extractor_func(msg)

        if reply_policy == cls.ReplyPolicy.only_origin:
            return message, await call_extractor(message)

        elif reply_policy == cls.ReplyPolicy.prefer_origin:
            result = await call_extractor(message)
            if result:
                return message, result

            if message.reply_to_message:
                reply_result = await call_extractor(message.reply_to_message)
                if reply_result:
                    return message.reply_to_message, reply_result

            return message, None

        elif reply_policy == cls.ReplyPolicy.prefer_reply:
            if message.reply_to_message:
                reply_result = await call_extractor(message.reply_to_message)
                if reply_result:
                    return message.reply_to_message, reply_result

            result = await call_extractor(message)
            return message, result

        elif reply_policy == cls.ReplyPolicy.only_reply:
            if message.reply_to_message:
                reply_result = await call_extractor(message.reply_to_message)
                return message.reply_to_message, reply_result

            return message, None

        return message, None

    @classmethod
    async def photo(
        cls,
        message: Message,
        with_profile_photo: bool = False,
        reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin,
    ) -> ExtractedPhoto | None:
        """
        Extract photo from message or its reply.

        Args:
            message: The message to extract from
            reply_policy: Policy for checking reply messages

        Returns:
            ExtractedPhoto object or None if no photo found
        """
        source_message, media = await cls._extract_with_policy(
            message, cls._extract_photo_from_message, reply_policy
        )
        if media:
            return ExtractedPhoto(message=source_message, media=media)

        if with_profile_photo:
            source_message, media = await cls._extract_with_policy(
                message,
                cls._extract_profile_photo_from_message,
                cls.ReplyPolicy.prefer_reply,
            )
            if media:
                return ExtractedPhoto(message=source_message, media=media)

        return None

    @classmethod
    async def video(
        cls, message: Message, reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin
    ) -> ExtractedVideo | None:
        """
        Extract video from message or its reply.

        Args:
            message: The message to extract from
            reply_policy: Policy for checking reply messages

        Returns:
            ExtractedVideo object or None if no video found
        """
        source_message, media = await cls._extract_with_policy(
            message, cls._extract_video_from_message, reply_policy
        )
        if media:
            return ExtractedVideo(message=source_message, media=media)
        return None

    @classmethod
    async def audio(
        cls, message: Message, reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin
    ) -> ExtractedAudio | None:
        """
        Extract audio from message or its reply.

        Args:
            message: The message to extract from
            reply_policy: Policy for checking reply messages

        Returns:
            ExtractedAudio object or None if no audio found
        """
        source_message, media = await cls._extract_with_policy(
            message, cls._extract_audio_from_message, reply_policy
        )
        if media:
            return ExtractedAudio(message=source_message, media=media)
        return None

    @classmethod
    async def document(
        cls, message: Message, reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin
    ) -> ExtractedDocument | None:
        """
        Extract document from message or its reply.

        Args:
            message: The message to extract from
            reply_policy: Policy for checking reply messages

        Returns:
            ExtractedDocument object or None if no document found
        """
        source_message, media = await cls._extract_with_policy(
            message, cls._extract_document_from_message, reply_policy
        )
        if media:
            return ExtractedDocument(message=source_message, media=media)
        return None

    @classmethod
    async def text(
        cls, message: Message, reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin
    ) -> ExtractedText | None:
        """
        Extract text from message or its reply (includes both text and caption).

        Args:
            message: The message to extract from
            reply_policy: Policy for checking reply messages

        Returns:
            ExtractedText object or None if no text found
        """
        source_message, text_content = await cls._extract_with_policy(
            message, cls._extract_text_from_message, reply_policy
        )
        if text_content:
            return ExtractedText(message=source_message, text=text_content)
        return None

    @classmethod
    async def all_media(
        cls, message: Message, reply_policy: ReplyPolicy = ReplyPolicy.prefer_origin
    ) -> tuple[
        ExtractedPhoto | None,
        ExtractedVideo | None,
        ExtractedAudio | None,
        ExtractedDocument | None,
        ExtractedText | None,
    ]:
        """
        Extract all media types from message or its reply in one call.

        Args:
            message: The message to extract from
            reply_policy: Policy for checking reply messages

        Returns:
            Tuple of (photo, video, audio, document, text) - any can be None
        """
        photo, video, audio, document, text = await asyncio.gather(
            cls.photo(message, reply_policy),
            cls.video(message, reply_policy),
            cls.audio(message, reply_policy),
            cls.document(message, reply_policy),
            cls.text(message, reply_policy),
        )
        return photo, video, audio, document, text
