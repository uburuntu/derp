import asyncio
from enum import IntEnum, auto

import httpx
import logfire
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
        return self.media.file_size

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
        with logfire.span(
            "media_download",
            **{
                "media.type": self.media_type,
                "media.file_size": self.file_size,
            },
        ) as span:
            try:
                download_url = await create_sensitive_url_from_file_id(
                    self.message.bot, self.file_id
                )
                async with httpx.AsyncClient() as client:
                    response = await client.get(download_url)
                    response.raise_for_status()
                    content = response.content
                    span.set_attribute("media.downloaded_bytes", len(content))
                    return content
            except Exception as e:
                raise RuntimeError("Failed to download media") from e


class ExtractedPhoto(ExtractedMedia):
    """Extracted photo content."""

    media: PhotoSize | Document | Sticker

    @property
    def width(self) -> int | None:
        """Get photo width if available."""
        return self.media.width

    @property
    def height(self) -> int | None:
        """Get photo height if available."""
        return self.media.height


class ExtractedVideo(ExtractedMedia):
    """Extracted video content."""

    media: Video | Animation | VideoNote | Sticker

    @property
    def duration(self) -> int | None:
        """Get video duration if available."""
        return self.media.duration

    @property
    def width(self) -> int | None:
        """Get video width if available."""
        return self.media.width

    @property
    def height(self) -> int | None:
        """Get video height if available."""
        return self.media.height


class ExtractedAudio(ExtractedMedia):
    """Extracted audio content."""

    media: Audio | Voice

    @property
    def duration(self) -> int | None:
        """Get audio duration if available."""
        return self.media.duration

    @property
    def title(self) -> str | None:
        """Get audio title if available."""
        return self.media.title

    @property
    def performer(self) -> str | None:
        """Get audio performer if available."""
        return self.media.performer


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
    """Unified extractor for media and text from Telegram messages.

    Provides a consistent API for extracting different content types (photos, videos,
    audio, documents, text) from messages, with configurable reply-checking behavior.

    All public methods return typed wrapper objects (ExtractedPhoto, ExtractedVideo, etc.)
    that provide:
    - Access to underlying media metadata (dimensions, duration, MIME type)
    - Async `download()` method to fetch raw bytes
    - Reference to the source message

    Key features:
    - Automatic reply fallback: By default, checks the replied-to message if no media
      found in the original (configurable via ReplyPolicy)
    - Profile photo support: `photo()` can fall back to user profile photos
    - Image-like document detection: Documents with image MIME types are treated as photos
    - Sticker handling: Static stickers → photos, video stickers → videos

    Example:
        >>> photo = await Extractor.photo(message, with_profile_photo=True)
        >>> if photo:
        ...     data = await photo.download()
        ...     print(f"Got {photo.width}x{photo.height} image, {len(data)} bytes")
    """

    class ReplyPolicy(IntEnum):
        """Controls how reply messages are checked when extracting media.

        Attributes:
            only_origin: Only check the original message, ignore any reply.
            prefer_origin: Check original first; if not found, check reply (default).
            prefer_reply: Check reply first; if not found, check original.
            only_reply: Only check the reply message, ignore the original.
        """

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
        """Extract photo content from a message, with reply and profile photo fallbacks.

        Checks for photo content in this order:
        1. Photo attachments (message.photo) - returns largest size
        2. Image documents (message.document with image/* MIME type)
        3. Static stickers (non-animated, non-video stickers)
        4. If with_profile_photo=True and nothing found:
           - Profile photo of forwarded user (if message is forwarded)
           - Profile photo of message author

        The reply_policy controls whether/how the replied-to message is checked.
        Default is prefer_origin: check original first, then reply.

        Args:
            message: The Telegram message to extract from.
            with_profile_photo: If True, fall back to user profile photo when no
                image is attached. Uses prefer_reply policy for profile lookup.
            reply_policy: How to handle reply messages. Defaults to prefer_origin.

        Returns:
            ExtractedPhoto with download() capability, or None if no photo found.

        Example:
            >>> # Get any image from message or its reply
            >>> photo = await Extractor.photo(message)
            >>> if photo:
            ...     data = await photo.download()
            >>>
            >>> # Get user's profile photo as fallback
            >>> photo = await Extractor.photo(message, with_profile_photo=True)
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
        """Extract video content from a message, with optional reply fallback.

        Checks for video content in this order:
        1. Video stickers (is_video=True stickers, excludes animated Lottie stickers)
        2. Regular videos (message.video)
        3. GIF animations (message.animation)
        4. Video notes / circles (message.video_note)

        Args:
            message: The Telegram message to extract from.
            reply_policy: How to handle reply messages. Defaults to prefer_origin.

        Returns:
            ExtractedVideo with duration, dimensions, and download() capability,
            or None if no video found.

        Example:
            >>> video = await Extractor.video(message)
            >>> if video:
            ...     print(f"{video.duration}s video at {video.width}x{video.height}")
            ...     data = await video.download()
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
        """Extract audio content from a message, with optional reply fallback.

        Checks for audio content:
        1. Audio files (message.audio) - music files with metadata
        2. Voice messages (message.voice)

        Args:
            message: The Telegram message to extract from.
            reply_policy: How to handle reply messages. Defaults to prefer_origin.

        Returns:
            ExtractedAudio with duration, title, performer, and download() capability,
            or None if no audio found.

        Example:
            >>> audio = await Extractor.audio(message)
            >>> if audio:
            ...     print(f"{audio.title} by {audio.performer}, {audio.duration}s")
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
        """Extract any document attachment from a message, with optional reply fallback.

        Returns the raw document without type interpretation. For image documents,
        consider using photo() instead, which automatically detects image/* MIME types.

        Args:
            message: The Telegram message to extract from.
            reply_policy: How to handle reply messages. Defaults to prefer_origin.

        Returns:
            ExtractedDocument with file_name, mime_type, and download() capability,
            or None if no document found.

        Example:
            >>> doc = await Extractor.document(message)
            >>> if doc and doc.mime_type == "application/pdf":
            ...     pdf_bytes = await doc.download()
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
        """Extract text content from a message, with optional reply fallback.

        Checks both message.text (plain text messages) and message.caption
        (text attached to media). Returns whichever is present.

        Args:
            message: The Telegram message to extract from.
            reply_policy: How to handle reply messages. Defaults to prefer_origin.

        Returns:
            ExtractedText with length, startswith(), contains() helpers,
            or None if no text found.

        Example:
            >>> text = await Extractor.text(message)
            >>> if text and text.startswith("/"):
            ...     print(f"Command: {text.text}")
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
        """Extract all media types from a message in parallel.

        Runs photo(), video(), audio(), document(), and text() concurrently
        using asyncio.gather. Useful when you need to inspect multiple media
        types from a single message without sequential await overhead.

        Note: Does not enable with_profile_photo for the photo extraction.

        Args:
            message: The Telegram message to extract from.
            reply_policy: How to handle reply messages. Defaults to prefer_origin.

        Returns:
            Tuple of (photo, video, audio, document, text). Each element is
            the corresponding Extracted* object or None if not present.

        Example:
            >>> photo, video, audio, doc, text = await Extractor.all_media(message)
            >>> if video:
            ...     print(f"Message has a {video.duration}s video")
        """
        photo, video, audio, document, text = await asyncio.gather(
            cls.photo(message, reply_policy),
            cls.video(message, reply_policy),
            cls.audio(message, reply_policy),
            cls.document(message, reply_policy),
            cls.text(message, reply_policy),
        )
        return photo, video, audio, document, text
