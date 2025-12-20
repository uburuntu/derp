"""Unified message sender with automatic text sanitization and chunking.

MessageSender provides a consistent interface for sending messages to Telegram
with automatic markdown-to-HTML conversion, text chunking for long messages,
and fallback to plain text on errors.

ContentBuilder provides a fluent API for composing mixed-content messages
(text, images, videos, audio) and sending them with proper Telegram API handling.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Self

import logfire
from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.media_group import MediaGroupBuilder
from pydantic_ai import BinaryContent

from derp.common.sanitize import sanitize_for_telegram

if TYPE_CHECKING:
    from pydantic_ai import BinaryImage

# Telegram limits
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_ALBUM_SIZE = 10


def _filename_from_mime(mime_type: str, idx: int = 1, prefix: str = "file") -> str:
    """Generate a filename from a MIME type.

    Args:
        mime_type: MIME type string (e.g., "image/jpeg", "video/mp4").
        idx: Index for numbered filenames (e.g., "image_1.jpg").
        prefix: Filename prefix (default: "file").

    Returns:
        Generated filename with appropriate extension.
    """
    mime_lower = mime_type.lower()

    # Image types
    if "jpeg" in mime_lower or "jpg" in mime_lower:
        return f"{prefix}_{idx}.jpg"
    if "png" in mime_lower:
        return f"{prefix}_{idx}.png"
    if "gif" in mime_lower:
        return f"{prefix}_{idx}.gif"
    if "webp" in mime_lower:
        return f"{prefix}_{idx}.webp"

    # Video types
    if "mp4" in mime_lower or "video" in mime_lower:
        return f"{prefix}_{idx}.mp4"
    if "webm" in mime_lower:
        return f"{prefix}_{idx}.webm"

    # Audio types
    if "mpeg" in mime_lower or "mp3" in mime_lower:
        return f"{prefix}_{idx}.mp3"
    if "ogg" in mime_lower:
        return f"{prefix}_{idx}.ogg"
    if "wav" in mime_lower:
        return f"{prefix}_{idx}.wav"

    # Default based on category
    if mime_lower.startswith("image/"):
        return f"{prefix}_{idx}.jpg"
    if mime_lower.startswith("video/"):
        return f"{prefix}_{idx}.mp4"
    if mime_lower.startswith("audio/"):
        return f"{prefix}_{idx}.mp3"

    return f"{prefix}_{idx}.bin"


class MediaType(str, Enum):
    """Supported media types for sending."""

    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    ANIMATION = "animation"
    DOCUMENT = "document"
    STICKER = "sticker"
    VIDEO_NOTE = "video_note"


@dataclass
class MediaItem:
    """A media item to be sent."""

    type: MediaType
    data: bytes | str  # bytes for upload, str for file_id or URL
    filename: str | None = None
    mime_type: str | None = None

    @classmethod
    def from_binary_image(cls, image: BinaryImage, idx: int = 1) -> MediaItem:
        """Create a MediaItem from a pydantic-ai BinaryImage."""
        return cls(
            type=MediaType.PHOTO,
            data=image.data,
            filename=_filename_from_mime(image.media_type, idx, "image"),
            mime_type=image.media_type,
        )

    @classmethod
    def from_binary_content(
        cls, content: BinaryContent, media_type: MediaType, idx: int = 1
    ) -> MediaItem:
        """Create a MediaItem from a pydantic-ai BinaryContent."""
        return cls(
            type=media_type,
            data=content.data,
            filename=_filename_from_mime(content.media_type, idx, media_type.value),
            mime_type=content.media_type,
        )

    def to_input_file(self) -> BufferedInputFile | str:
        """Convert to aiogram input file or return file_id/URL string."""
        if isinstance(self.data, bytes):
            return BufferedInputFile(
                file=self.data,
                filename=self.filename or self._default_filename(),
            )
        return self.data

    def _default_filename(self) -> str:
        """Generate default filename based on type."""
        if self.mime_type:
            return _filename_from_mime(self.mime_type, 1, self.type.value)
        ext_map = {
            MediaType.PHOTO: "jpg",
            MediaType.VIDEO: "mp4",
            MediaType.AUDIO: "mp3",
            MediaType.VOICE: "ogg",
            MediaType.ANIMATION: "gif",
            MediaType.DOCUMENT: "file",
            MediaType.STICKER: "webp",
            MediaType.VIDEO_NOTE: "mp4",
        }
        return f"file.{ext_map.get(self.type, 'bin')}"


ReplyMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None


@dataclass
class ContentBuilder:
    """Fluent builder for composing mixed-content messages.

    Accumulates text, images, videos, and audio, then sends everything
    with proper Telegram API handling (albums grouped by type, chunking, etc.).

    Usage:
        await sender.compose().text("Hello").images(result.images).reply()
        await sender.compose().text("Caption").video(video_bytes).reply()
    """

    _sender: MessageSender
    _text: str | None = field(default=None, repr=False)
    _images: list[MediaItem] = field(default_factory=list, repr=False)
    _videos: list[MediaItem] = field(default_factory=list, repr=False)
    _audio: list[MediaItem] = field(default_factory=list, repr=False)
    _documents: list[MediaItem] = field(default_factory=list, repr=False)
    _reply_markup: ReplyMarkup = field(default=None, repr=False)

    def text(self, text: str) -> Self:
        """Set the text content (also used as caption for media)."""
        self._text = text
        return self

    def image(
        self, image: BinaryImage | BinaryContent | bytes, mime_type: str = "image/jpeg"
    ) -> Self:
        """Add a single image."""
        if isinstance(image, bytes):
            self._images.append(
                MediaItem(
                    type=MediaType.PHOTO,
                    data=image,
                    filename=_filename_from_mime(
                        mime_type, len(self._images) + 1, "image"
                    ),
                    mime_type=mime_type,
                )
            )
        else:
            self._images.append(
                MediaItem(
                    type=MediaType.PHOTO,
                    data=image.data,
                    filename=_filename_from_mime(
                        image.media_type, len(self._images) + 1, "image"
                    ),
                    mime_type=image.media_type,
                )
            )
        return self

    def images(self, images: list[BinaryImage] | list[bytes]) -> Self:
        """Add multiple images."""
        for img in images:
            self.image(img)
        return self

    def video(self, video: BinaryContent | bytes, mime_type: str = "video/mp4") -> Self:
        """Add a single video."""
        if isinstance(video, bytes):
            self._videos.append(
                MediaItem(
                    type=MediaType.VIDEO,
                    data=video,
                    filename=_filename_from_mime(
                        mime_type, len(self._videos) + 1, "video"
                    ),
                    mime_type=mime_type,
                )
            )
        else:
            self._videos.append(
                MediaItem(
                    type=MediaType.VIDEO,
                    data=video.data,
                    filename=_filename_from_mime(
                        video.media_type, len(self._videos) + 1, "video"
                    ),
                    mime_type=video.media_type,
                )
            )
        return self

    def audio(
        self, audio: BinaryContent | bytes, mime_type: str = "audio/mpeg"
    ) -> Self:
        """Add a single audio file."""
        if isinstance(audio, bytes):
            self._audio.append(
                MediaItem(
                    type=MediaType.AUDIO,
                    data=audio,
                    filename=_filename_from_mime(
                        mime_type, len(self._audio) + 1, "audio"
                    ),
                    mime_type=mime_type,
                )
            )
        else:
            self._audio.append(
                MediaItem(
                    type=MediaType.AUDIO,
                    data=audio.data,
                    filename=_filename_from_mime(
                        audio.media_type, len(self._audio) + 1, "audio"
                    ),
                    mime_type=audio.media_type,
                )
            )
        return self

    def document(
        self,
        document: BinaryContent | bytes,
        mime_type: str = "application/octet-stream",
    ) -> Self:
        """Add a document."""
        if isinstance(document, bytes):
            self._documents.append(
                MediaItem(
                    type=MediaType.DOCUMENT,
                    data=document,
                    filename=_filename_from_mime(
                        mime_type, len(self._documents) + 1, "document"
                    ),
                    mime_type=mime_type,
                )
            )
        else:
            self._documents.append(
                MediaItem(
                    type=MediaType.DOCUMENT,
                    data=document.data,
                    filename=_filename_from_mime(
                        document.media_type, len(self._documents) + 1, "document"
                    ),
                    mime_type=document.media_type,
                )
            )
        return self

    def markup(self, reply_markup: ReplyMarkup) -> Self:
        """Set the reply markup."""
        self._reply_markup = reply_markup
        return self

    async def send(self, reply_to: Message | None = None) -> Message | list[Message]:
        """Send the accumulated content.

        Sending strategy:
        1. If only text: send as text message(s) with chunking
        2. If single media + short text: send as captioned media
        3. If multiple media: send as album(s) with caption on first
        4. Different media types are sent as separate albums

        Returns:
            The last sent Message, or list of all Messages.
        """
        return await self._sender._send_composed_content(
            text=self._text,
            images=self._images,
            videos=self._videos,
            audio=self._audio,
            documents=self._documents,
            reply_to=reply_to,
            reply_markup=self._reply_markup,
        )

    async def reply(self) -> Message | list[Message]:
        """Reply to the source message with the accumulated content."""
        if self._sender._source_message is None:
            raise ValueError(
                "Cannot reply without source message. "
                "Use ContentBuilder.send() instead."
            )
        return await self.send(reply_to=self._sender._source_message)


def _split_text(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks at natural breakpoints.

    Tries to split at (in order of preference):
    1. Double newlines (paragraphs)
    2. Single newlines
    3. Sentence endings (. ! ?)
    4. Spaces
    5. Hard cut at max_len if no breakpoint found

    Args:
        text: Text to split.
        max_len: Maximum length per chunk (default: 4096 for messages).

    Returns:
        List of text chunks, each <= max_len characters.
    """
    if not text or len(text) <= max_len:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to find a good breakpoint
        chunk = remaining[:max_len]

        # Try paragraph break first
        break_pos = chunk.rfind("\n\n")
        if break_pos == -1 or break_pos < max_len // 2:
            # Try single newline
            break_pos = chunk.rfind("\n")
        if break_pos == -1 or break_pos < max_len // 2:
            # Try sentence ending
            for ending in (". ", "! ", "? "):
                pos = chunk.rfind(ending)
                if pos > max_len // 2:
                    break_pos = pos + 1  # Include the punctuation
                    break
        if break_pos == -1 or break_pos < max_len // 2:
            # Try space
            break_pos = chunk.rfind(" ")
        if break_pos == -1 or break_pos < max_len // 4:
            # Hard cut - no good breakpoint found
            break_pos = max_len

        chunks.append(remaining[:break_pos].rstrip())
        remaining = remaining[break_pos:].lstrip()

    return [c for c in chunks if c]  # Filter empty chunks


@dataclass
class MessageSender:
    """Unified message sender with automatic text sanitization and chunking.

    All text is automatically:
    - Sanitized (markdown converted to HTML)
    - Chunked if over 4096 characters (multiple messages sent)
    - Fallen back to plain text if HTML parsing fails

    Usage with middleware (recommended):
        async def my_handler(message: Message, sender: MessageSender):
            await sender.reply("Hello **world**!")  # Just works

    Manual usage:
        sender = MessageSender.from_message(message)
        await sender.reply("Response text")
    """

    bot: Bot
    chat_id: int
    thread_id: int | None = None
    business_connection_id: str | None = None
    reply_markup: ReplyMarkup = None
    disable_notification: bool | None = None
    protect_content: bool | None = None
    disable_web_page_preview: bool = True

    # Internal state
    _sanitize: bool = field(default=True, repr=False)
    _source_message: Message | None = field(default=None, repr=False)

    @classmethod
    def from_message(cls, message: Message, **kwargs) -> MessageSender:
        """Create a MessageSender from a Message object.

        Extracts chat_id, thread_id, and business_connection_id from the message.
        The message is stored internally for reply() to use.
        """
        return cls(
            bot=message.bot,
            chat_id=message.chat.id,
            thread_id=message.message_thread_id,
            business_connection_id=getattr(message, "business_connection_id", None),
            _source_message=message,
            **kwargs,
        )

    def compose(self) -> ContentBuilder:
        """Create a ContentBuilder for composing mixed-content messages.

        Usage:
            await sender.compose().text("Hello").images(images).reply()
        """
        return ContentBuilder(_sender=self)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    @property
    def _common_send_kwargs(self) -> dict[str, Any]:
        """Common kwargs for all bot.send_* methods."""
        return {
            "chat_id": self.chat_id,
            "message_thread_id": self.thread_id,
            "business_connection_id": self.business_connection_id,
            "disable_notification": self.disable_notification,
            "protect_content": self.protect_content,
        }

    def _prepare_text(self, text: str) -> str:
        """Prepare text for sending by sanitizing if enabled."""
        if self._sanitize and text:
            return sanitize_for_telegram(text)
        return text

    def _prepare_caption(self, caption: str | None) -> tuple[str | None, str | None]:
        """Prepare caption, splitting if too long for a single media message.

        Args:
            caption: Raw caption text (may contain markdown).

        Returns:
            (media_caption, overflow_text) tuple:
            - media_caption: First chunk (<=1024 chars) to attach to media
            - overflow_text: Remainder to send as follow-up text messages
        """
        if not caption:
            return None, None

        prepared = self._prepare_text(caption)
        if len(prepared) <= MAX_CAPTION_LENGTH:
            return prepared, None

        chunks = _split_text(prepared, MAX_CAPTION_LENGTH)
        media_caption = chunks[0] if chunks else None
        overflow = "\n".join(chunks[1:]) if len(chunks) > 1 else None
        return media_caption, overflow

    async def _send_overflow_messages(
        self,
        overflow_text: str,
        after_message: Message,
        reply_markup: ReplyMarkup = None,
    ) -> list[Message]:
        """Send overflow text as chained follow-up messages.

        Args:
            overflow_text: Text that didn't fit in the media caption.
            after_message: Message to chain replies from.
            reply_markup: Keyboard to attach to the last message.

        Returns:
            List of sent follow-up messages.
        """
        messages: list[Message] = []
        chunks = _split_text(overflow_text)
        last_msg = after_message

        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            last_msg = await self._send_single_message(
                chunk,
                reply_to_message_id=last_msg.message_id,
                reply_markup=reply_markup if is_last else None,
            )
            messages.append(last_msg)

        return messages

    async def _send_single_message(
        self,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> Message:
        """Send a single message (HTML fallback handled by middleware)."""
        return await self.bot.send_message(
            **self._common_send_kwargs,
            text=text,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

    async def _send_captioned_media(
        self,
        send_method: Callable[..., Awaitable[Message]],
        media_key: str,
        media_file: BufferedInputFile | str,
        *,
        caption: str | None = None,
        reply_to: Message | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Generic sender for captioned media (photo, video, audio).

        Handles caption preparation and splitting at 1024 chars.
        HTML parse error fallback is handled by ResilientRequestMiddleware.
        """
        media_caption, overflow_text = self._prepare_caption(caption)
        sent_messages: list[Message] = []

        msg = await send_method(
            **self._common_send_kwargs,
            **{media_key: media_file},
            caption=media_caption,
            parse_mode="HTML" if media_caption else None,
            reply_to_message_id=reply_to.message_id if reply_to else None,
            reply_markup=reply_markup if not overflow_text else None,
        )
        sent_messages.append(msg)

        if overflow_text:
            overflow_msgs = await self._send_overflow_messages(
                overflow_text,
                sent_messages[-1],
                reply_markup=reply_markup,
            )
            sent_messages.extend(overflow_msgs)

        return sent_messages[0] if len(sent_messages) == 1 else sent_messages

    # -------------------------------------------------------------------------
    # Text messages
    # -------------------------------------------------------------------------

    async def send(
        self,
        text: str,
        *,
        reply_markup: ReplyMarkup = None,
    ) -> Message:
        """Send a text message, automatically chunking if needed.

        Args:
            text: The message text (markdown will be converted to HTML).
            reply_markup: Optional keyboard markup (attached to last message).

        Returns:
            The last sent Message.
        """
        prepared_text = self._prepare_text(text)
        chunks = _split_text(prepared_text)

        if not chunks:
            raise ValueError("Cannot send empty message")

        last_message: Message | None = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            last_message = await self._send_single_message(
                chunk,
                reply_to_message_id=last_message.message_id if last_message else None,
                reply_markup=reply_markup if is_last else None,
            )

        return last_message  # type: ignore[return-value]

    async def reply(
        self,
        text: str,
        *,
        reply_markup: ReplyMarkup = None,
    ) -> Message:
        """Reply to the source message, automatically chunking if needed.

        Args:
            text: The reply text (markdown will be converted to HTML).
            reply_markup: Optional keyboard markup (attached to last message).

        Returns:
            The last sent Message.

        Raises:
            ValueError: If no source message is available.
        """
        if self._source_message is None:
            raise ValueError(
                "Cannot reply without source message. "
                "Use MessageSender.from_message() or send() instead."
            )

        prepared_text = self._prepare_text(text)
        chunks = _split_text(prepared_text)

        if not chunks:
            raise ValueError("Cannot send empty message")

        last_message: Message | None = None
        for i, chunk in enumerate(chunks):
            is_first = i == 0
            is_last = i == len(chunks) - 1

            if is_first:
                last_message = await self._source_message.reply(
                    text=chunk,
                    parse_mode="HTML",
                    reply_markup=reply_markup if is_last else None,
                    disable_notification=self.disable_notification,
                )
            else:
                last_message = await self._send_single_message(
                    chunk,
                    reply_to_message_id=last_message.message_id
                    if last_message
                    else None,
                    reply_markup=reply_markup if is_last else None,
                )

        return last_message  # type: ignore[return-value]

    # -------------------------------------------------------------------------
    # Edit messages
    # -------------------------------------------------------------------------

    async def edit(
        self,
        message: Message,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | bool:
        """Edit a message's text (HTML fallback handled by middleware).

        Note: Editing does not support chunking - text must fit in one message.
        """
        prepared_text = self._prepare_text(text)

        # Truncate if too long (editing doesn't support multi-message)
        if len(prepared_text) > MAX_MESSAGE_LENGTH:
            prepared_text = prepared_text[: MAX_MESSAGE_LENGTH - 3] + "..."

        return await message.edit_text(
            text=prepared_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def edit_inline(
        self,
        inline_message_id: str,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Edit an inline message's text (HTML fallback handled by middleware).

        Note: Editing does not support chunking - text must fit in one message.
        """
        prepared_text = self._prepare_text(text)

        if len(prepared_text) > MAX_MESSAGE_LENGTH:
            prepared_text = prepared_text[: MAX_MESSAGE_LENGTH - 3] + "..."

        return await self.bot.edit_message_text(
            text=prepared_text,
            inline_message_id=inline_message_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    # -------------------------------------------------------------------------
    # Composed content (for ContentBuilder)
    # -------------------------------------------------------------------------

    async def _send_composed_content(
        self,
        *,
        text: str | None,
        images: list[MediaItem],
        videos: list[MediaItem],
        audio: list[MediaItem],
        documents: list[MediaItem],
        reply_to: Message | None,
        reply_markup: ReplyMarkup,
    ) -> Message | list[Message]:
        """Internal method to send composed content from ContentBuilder.

        Sending strategy:
        1. If only text: send as text message(s) with chunking
        2. If single media + short text: send as captioned media
        3. If multiple media of same type: send as album with caption
        4. Different media types are sent as separate albums
        """
        all_sent: list[Message] = []
        has_media = images or videos or audio or documents

        # Case 1: Text only
        if not has_media:
            if not text:
                raise ValueError("No content to send")
            prepared = self._prepare_text(text)
            chunks = _split_text(prepared)
            reply_to_id = reply_to.message_id if reply_to else None

            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                msg = await self._send_single_message(
                    chunk,
                    reply_to_message_id=reply_to_id,
                    reply_markup=reply_markup if is_last else None,
                )
                all_sent.append(msg)
                reply_to_id = msg.message_id

            return all_sent[-1] if len(all_sent) == 1 else all_sent

        # Case 2: Single media with short caption
        total_media = len(images) + len(videos) + len(audio) + len(documents)
        if total_media == 1 and (not text or len(text) <= MAX_CAPTION_LENGTH):
            # Find the single item
            item = (images or videos or audio or documents)[0]
            send_method, media_key = {
                MediaType.PHOTO: (self.bot.send_photo, "photo"),
                MediaType.VIDEO: (self.bot.send_video, "video"),
                MediaType.AUDIO: (self.bot.send_audio, "audio"),
                MediaType.DOCUMENT: (self.bot.send_document, "document"),
            }.get(item.type, (self.bot.send_document, "document"))

            return await self._send_captioned_media(
                send_method,
                media_key,
                item.to_input_file(),
                caption=text,
                reply_to=reply_to,
                reply_markup=reply_markup,
            )

        # Case 3: Multiple media - send as albums by type, caption on first
        last_reply_to = reply_to
        caption_used = False

        # Send each media type as separate album(s)
        for media_list in [images, videos, audio, documents]:
            if not media_list:
                continue

            album_caption = text if not caption_used else None
            msgs = await self._send_typed_album(
                media_list,
                caption=album_caption,
                reply_to=last_reply_to,
            )
            if msgs:
                all_sent.extend(msgs)
                last_reply_to = msgs[-1]
                if album_caption:
                    caption_used = True

        return all_sent[-1] if len(all_sent) == 1 else all_sent

    async def _send_typed_album(
        self,
        media: list[MediaItem],
        *,
        caption: str | None = None,
        reply_to: Message | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> list[Message]:
        """Send a homogeneous media album (HTML fallback handled by middleware).

        Splits into chunks of MAX_ALBUM_SIZE (10) if needed.
        """
        if not media:
            return []

        album_caption, overflow_text = self._prepare_caption(caption)
        all_messages: list[Message] = []

        for chunk_idx, chunk_start in enumerate(range(0, len(media), MAX_ALBUM_SIZE)):
            chunk = media[chunk_start : chunk_start + MAX_ALBUM_SIZE]
            builder = MediaGroupBuilder(
                caption=album_caption if chunk_idx == 0 else None
            )

            for item in chunk:
                input_file = item.to_input_file()
                match item.type:
                    case MediaType.PHOTO:
                        builder.add_photo(media=input_file)
                    case MediaType.VIDEO:
                        builder.add_video(media=input_file)
                    case MediaType.AUDIO:
                        builder.add_audio(media=input_file)
                    case MediaType.DOCUMENT:
                        builder.add_document(media=input_file)
                    case _:
                        logfire.warning(
                            "unsupported_album_type",
                            type=item.type.value,
                        )
                        continue

            messages = await self.bot.send_media_group(
                **self._common_send_kwargs,
                media=builder.build(),
                reply_to_message_id=reply_to.message_id if reply_to else None,
            )
            all_messages.extend(messages)

        if overflow_text and all_messages:
            overflow_msgs = await self._send_overflow_messages(
                overflow_text, all_messages[-1], reply_markup=reply_markup
            )
            all_messages.extend(overflow_msgs)

        return all_messages
