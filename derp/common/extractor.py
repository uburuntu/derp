import asyncio
from enum import IntEnum, auto

import httpx
from aiogram.types import (
    Animation,
    Audio,
    Document,
    Message,
    PhotoSize,
    Sticker,
    Video,
    VideoNote,
    Voice,
)
from pydantic import BaseModel

# Reuse helpers from tg module to avoid duplication
from .tg import create_sensitive_url_from_file_id, profile_photo


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
        reply_policy: "Extractor.ReplyPolicy" = None,
    ):
        """Extract content using the specified reply policy."""

        # default to prefer_origin if not provided (for easier external use)
        reply_policy = reply_policy or cls.ReplyPolicy.prefer_origin

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
        reply_policy: "Extractor.ReplyPolicy" = None,
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
        cls, message: Message, reply_policy: "Extractor.ReplyPolicy" = None
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
        cls, message: Message, reply_policy: "Extractor.ReplyPolicy" = None
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
        cls, message: Message, reply_policy: "Extractor.ReplyPolicy" = None
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
        cls, message: Message, reply_policy: "Extractor.ReplyPolicy" = None
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
        cls, message: Message, reply_policy: "Extractor.ReplyPolicy" = None
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
