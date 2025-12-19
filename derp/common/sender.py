"""Unified message sender with automatic text sanitization and chunking.

MessageSender provides a consistent interface for sending messages to Telegram
with automatic markdown-to-HTML conversion, text chunking for long messages,
and fallback to plain text on errors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import logfire
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.media_group import MediaGroupBuilder

from derp.common.sanitize import sanitize_for_telegram, strip_html_tags

# Telegram limits
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_ALBUM_SIZE = 10


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
        """Send a single message with HTML fallback on parse error."""
        try:
            return await self.bot.send_message(
                **self._common_send_kwargs,
                text=text,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if "can't parse entities" in exc.message.lower():
                logfire.warning(
                    "html_parse_failed_fallback",
                    error=exc.message,
                    text_preview=text[:100] if text else None,
                )
                return await self.bot.send_message(
                    **self._common_send_kwargs,
                    text=strip_html_tags(text) if text else "",
                    parse_mode=None,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                )
            raise

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

        Handles:
        - Caption preparation and splitting at 1024 chars
        - HTML parse error fallback to plain text
        - Overflow text as follow-up messages

        Args:
            send_method: Bot method to call (e.g., self.bot.send_photo).
            media_key: Parameter name for the media (e.g., "photo", "video").
            media_file: The file to send.
            caption: Optional caption text.
            reply_to: Optional message to reply to.
            reply_markup: Optional keyboard markup.

        Returns:
            Single Message or list[Message] if caption was split.
        """
        media_caption, overflow_text = self._prepare_caption(caption)
        sent_messages: list[Message] = []

        send_kwargs = {
            **self._common_send_kwargs,
            media_key: media_file,
            "caption": media_caption,
            "parse_mode": "HTML" if media_caption else None,
            "reply_to_message_id": reply_to.message_id if reply_to else None,
            "reply_markup": reply_markup if not overflow_text else None,
        }

        try:
            msg = await send_method(**send_kwargs)
            sent_messages.append(msg)
        except TelegramBadRequest as exc:
            if "can't parse entities" in exc.message.lower() and media_caption:
                logfire.warning(
                    "html_parse_failed_fallback",
                    error=exc.message,
                    media_type=media_key,
                )
                send_kwargs["caption"] = strip_html_tags(media_caption)
                send_kwargs["parse_mode"] = None
                msg = await send_method(**send_kwargs)
                sent_messages.append(msg)
            else:
                raise

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

            try:
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
            except TelegramBadRequest as exc:
                if "can't parse entities" in exc.message.lower():
                    logfire.warning(
                        "html_parse_failed_fallback",
                        error=exc.message,
                        text_preview=chunk[:100] if chunk else None,
                    )
                    plain_chunk = strip_html_tags(chunk)
                    if is_first:
                        last_message = await self._source_message.reply(
                            text=plain_chunk,
                            parse_mode=None,
                            reply_markup=reply_markup if is_last else None,
                            disable_notification=self.disable_notification,
                        )
                    else:
                        last_message = await self.bot.send_message(
                            **self._common_send_kwargs,
                            text=plain_chunk,
                            parse_mode=None,
                            reply_to_message_id=last_message.message_id
                            if last_message
                            else None,
                            reply_markup=reply_markup if is_last else None,
                        )
                else:
                    raise

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
        """Edit a message's text.

        Note: Editing does not support chunking - text must fit in one message.

        Args:
            message: The message to edit.
            text: The new text (markdown will be converted to HTML).
            reply_markup: Optional inline keyboard markup.

        Returns:
            The edited Message or True for inline messages.
        """
        prepared_text = self._prepare_text(text)

        # Truncate if too long (editing doesn't support multi-message)
        if len(prepared_text) > MAX_MESSAGE_LENGTH:
            prepared_text = prepared_text[: MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            return await message.edit_text(
                text=prepared_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if "can't parse entities" in exc.message.lower():
                logfire.warning(
                    "html_parse_failed_fallback",
                    error=exc.message,
                    text_preview=prepared_text[:100] if prepared_text else None,
                )
                return await message.edit_text(
                    text=strip_html_tags(prepared_text),
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
            raise

    async def edit_inline(
        self,
        inline_message_id: str,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Edit an inline message's text.

        Note: Editing does not support chunking - text must fit in one message.

        Args:
            inline_message_id: The inline message ID.
            text: The new text (markdown will be converted to HTML).
            reply_markup: Optional inline keyboard markup.

        Returns:
            True on success.
        """
        prepared_text = self._prepare_text(text)

        if len(prepared_text) > MAX_MESSAGE_LENGTH:
            prepared_text = prepared_text[: MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            return await self.bot.edit_message_text(
                text=prepared_text,
                inline_message_id=inline_message_id,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if "can't parse entities" in exc.message.lower():
                logfire.warning(
                    "html_parse_failed_fallback_inline",
                    error=exc.message,
                    text_preview=prepared_text[:100] if prepared_text else None,
                )
                return await self.bot.edit_message_text(
                    text=strip_html_tags(prepared_text),
                    inline_message_id=inline_message_id,
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
            raise

    # -------------------------------------------------------------------------
    # Media: Photo
    # -------------------------------------------------------------------------

    async def send_photo(
        self,
        photo: bytes | str,
        *,
        caption: str | None = None,
        filename: str = "photo.jpg",
        reply_to: Message | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Send a photo with optional caption.

        If caption exceeds 1024 characters, overflow is sent as follow-up messages.
        """
        input_file = (
            BufferedInputFile(file=photo, filename=filename)
            if isinstance(photo, bytes)
            else photo
        )
        return await self._send_captioned_media(
            self.bot.send_photo,
            "photo",
            input_file,
            caption=caption,
            reply_to=reply_to,
            reply_markup=reply_markup,
        )

    async def reply_photo(
        self,
        photo: bytes | str,
        *,
        caption: str | None = None,
        filename: str = "photo.jpg",
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Reply with a photo."""
        return await self.send_photo(
            photo,
            caption=caption,
            filename=filename,
            reply_to=self._source_message,
            reply_markup=reply_markup,
        )

    # -------------------------------------------------------------------------
    # Media: Video
    # -------------------------------------------------------------------------

    async def send_video(
        self,
        video: bytes | str,
        *,
        caption: str | None = None,
        filename: str = "video.mp4",
        reply_to: Message | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Send a video with optional caption.

        If caption exceeds 1024 characters, overflow is sent as follow-up messages.
        """
        input_file = (
            BufferedInputFile(file=video, filename=filename)
            if isinstance(video, bytes)
            else video
        )
        return await self._send_captioned_media(
            self.bot.send_video,
            "video",
            input_file,
            caption=caption,
            reply_to=reply_to,
            reply_markup=reply_markup,
        )

    async def reply_video(
        self,
        video: bytes | str,
        *,
        caption: str | None = None,
        filename: str = "video.mp4",
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Reply with a video."""
        return await self.send_video(
            video,
            caption=caption,
            filename=filename,
            reply_to=self._source_message,
            reply_markup=reply_markup,
        )

    # -------------------------------------------------------------------------
    # Media: Audio
    # -------------------------------------------------------------------------

    async def send_audio(
        self,
        audio: bytes | str,
        *,
        caption: str | None = None,
        filename: str = "audio.mp3",
        reply_to: Message | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Send an audio file with optional caption.

        If caption exceeds 1024 characters, overflow is sent as follow-up messages.
        """
        input_file = (
            BufferedInputFile(file=audio, filename=filename)
            if isinstance(audio, bytes)
            else audio
        )
        return await self._send_captioned_media(
            self.bot.send_audio,
            "audio",
            input_file,
            caption=caption,
            reply_to=reply_to,
            reply_markup=reply_markup,
        )

    async def reply_audio(
        self,
        audio: bytes | str,
        *,
        caption: str | None = None,
        filename: str = "audio.mp3",
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Reply with an audio file."""
        return await self.send_audio(
            audio,
            caption=caption,
            filename=filename,
            reply_to=self._source_message,
            reply_markup=reply_markup,
        )

    # -------------------------------------------------------------------------
    # Media groups
    # -------------------------------------------------------------------------

    async def send_media_group(
        self,
        media: list[MediaItem],
        *,
        caption: str | None = None,
        reply_to: Message | None = None,
    ) -> list[Message]:
        """Send a media group (album).

        If caption exceeds 1024 characters, overflow is sent as follow-up messages.

        Args:
            media: List of MediaItem objects.
            caption: Optional caption for the first item.
            reply_to: Optional message to reply to.

        Returns:
            List of sent Messages.
        """
        if not media:
            return []

        album_caption, overflow_text = self._prepare_caption(caption)
        all_messages: list[Message] = []

        # Process in chunks of 10 (Telegram limit)
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
                            "unsupported_media_group_type",
                            type=item.type.value,
                        )
                        continue

            try:
                messages = await self.bot.send_media_group(
                    **self._common_send_kwargs,
                    media=builder.build(),
                    reply_to_message_id=reply_to.message_id if reply_to else None,
                )
                all_messages.extend(messages)
            except TelegramBadRequest as exc:
                if "can't parse entities" in exc.message.lower() and album_caption:
                    # Rebuild with plain caption
                    builder_plain = MediaGroupBuilder(
                        caption=strip_html_tags(album_caption)
                        if chunk_idx == 0
                        else None
                    )
                    for item in chunk:
                        input_file = item.to_input_file()
                        match item.type:
                            case MediaType.PHOTO:
                                builder_plain.add_photo(media=input_file)
                            case MediaType.VIDEO:
                                builder_plain.add_video(media=input_file)
                            case MediaType.AUDIO:
                                builder_plain.add_audio(media=input_file)
                            case MediaType.DOCUMENT:
                                builder_plain.add_document(media=input_file)

                    messages = await self.bot.send_media_group(
                        **self._common_send_kwargs,
                        media=builder_plain.build(),
                        reply_to_message_id=reply_to.message_id if reply_to else None,
                    )
                    all_messages.extend(messages)
                else:
                    raise

        # Send overflow text as follow-up messages
        if overflow_text and all_messages:
            overflow_msgs = await self._send_overflow_messages(
                overflow_text, all_messages[-1]
            )
            all_messages.extend(overflow_msgs)

        return all_messages

    # -------------------------------------------------------------------------
    # Mixed content
    # -------------------------------------------------------------------------

    async def send_with_media(
        self,
        text: str,
        media: list[MediaItem],
        *,
        reply_to: Message | None = None,
        reply_markup: ReplyMarkup = None,
    ) -> Message | list[Message]:
        """Send text with optional media attachments.

        Smart handling:
        - Single media + short text (<=1024): sends as captioned media
        - Multiple media: sends as media group with caption on first
        - Text only: sends as text message(s), auto-chunked
        - Text + media (long text): sends text first, then media

        Args:
            text: The message text.
            media: List of media items to attach.
            reply_to: Optional message to reply to.
            reply_markup: Optional keyboard markup.

        Returns:
            The sent Message(s).
        """
        prepared_text = self._prepare_text(text) if text else ""

        # No media - just send text (with chunking)
        if not media:
            if not prepared_text:
                raise ValueError("Either text or media must be provided")

            chunks = _split_text(prepared_text)
            sent_messages: list[Message] = []
            reply_to_id = reply_to.message_id if reply_to else None

            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                msg = await self._send_single_message(
                    chunk,
                    reply_to_message_id=reply_to_id,
                    reply_markup=reply_markup if is_last else None,
                )
                sent_messages.append(msg)
                reply_to_id = msg.message_id

            return sent_messages[0] if len(sent_messages) == 1 else sent_messages

        # Single media + short text - send as captioned media
        if len(media) == 1 and len(prepared_text) <= MAX_CAPTION_LENGTH:
            item = media[0]
            input_file = item.to_input_file()

            send_method = {
                MediaType.PHOTO: self.bot.send_photo,
                MediaType.VIDEO: self.bot.send_video,
                MediaType.AUDIO: self.bot.send_audio,
                MediaType.VOICE: self.bot.send_voice,
                MediaType.ANIMATION: self.bot.send_animation,
                MediaType.DOCUMENT: self.bot.send_document,
                MediaType.VIDEO_NOTE: self.bot.send_video_note,
                MediaType.STICKER: self.bot.send_sticker,
            }.get(item.type)

            if not send_method:
                logfire.warning("unknown_media_type", type=item.type.value)
                return await self.send(prepared_text, reply_markup=reply_markup)

            # Handle media types that don't support captions
            if item.type in {MediaType.STICKER, MediaType.VIDEO_NOTE}:
                sent: list[Message] = []
                if prepared_text:
                    text_msg = await self._send_single_message(
                        prepared_text,
                        reply_to_message_id=reply_to.message_id if reply_to else None,
                    )
                    sent.append(text_msg)
                    reply_to = text_msg

                media_key = (
                    "sticker" if item.type == MediaType.STICKER else "video_note"
                )
                media_msg = await send_method(
                    **self._common_send_kwargs,
                    **{media_key: input_file},
                    reply_markup=reply_markup or self.reply_markup,
                )
                sent.append(media_msg)
                return sent[-1] if len(sent) == 1 else sent

            media_key = {
                MediaType.PHOTO: "photo",
                MediaType.VIDEO: "video",
                MediaType.AUDIO: "audio",
                MediaType.VOICE: "voice",
                MediaType.ANIMATION: "animation",
                MediaType.DOCUMENT: "document",
            }.get(item.type, "document")

            try:
                return await send_method(
                    **self._common_send_kwargs,
                    **{media_key: input_file},
                    caption=prepared_text or None,
                    parse_mode="HTML" if prepared_text else None,
                    reply_to_message_id=reply_to.message_id if reply_to else None,
                    reply_markup=reply_markup or self.reply_markup,
                )
            except TelegramBadRequest as exc:
                if "can't parse entities" in exc.message.lower() and prepared_text:
                    return await send_method(
                        **self._common_send_kwargs,
                        **{media_key: input_file},
                        caption=strip_html_tags(prepared_text) or None,
                        parse_mode=None,
                        reply_to_message_id=reply_to.message_id if reply_to else None,
                        reply_markup=reply_markup or self.reply_markup,
                    )
                raise

        # Multiple media or long text - send text first, then media group
        sent_messages: list[Message] = []

        if prepared_text:
            text_chunks = _split_text(prepared_text)
            last_msg: Message | None = None
            for chunk in text_chunks:
                last_msg = await self._send_single_message(
                    chunk,
                    reply_to_message_id=reply_to.message_id
                    if reply_to and last_msg is None
                    else (last_msg.message_id if last_msg else None),
                    reply_markup=None,  # Markup on media
                )
                sent_messages.append(last_msg)
            reply_to = last_msg

        # Group media by type for sending
        groupable_media = [
            m for m in media if m.type in {MediaType.PHOTO, MediaType.VIDEO}
        ]
        if groupable_media:
            media_msgs = await self.send_media_group(
                media=groupable_media,
                reply_to=reply_to,
            )
            sent_messages.extend(media_msgs)

        # Send non-groupable media individually
        for item in media:
            if item.type not in {MediaType.PHOTO, MediaType.VIDEO}:
                input_file = item.to_input_file()
                reply_to_id = sent_messages[-1].message_id if sent_messages else None

                match item.type:
                    case MediaType.AUDIO:
                        msg = await self.bot.send_audio(
                            **self._common_send_kwargs,
                            audio=input_file,
                            reply_to_message_id=reply_to_id,
                        )
                        sent_messages.append(msg)
                    case MediaType.VOICE:
                        msg = await self.bot.send_voice(
                            **self._common_send_kwargs,
                            voice=input_file,
                            reply_to_message_id=reply_to_id,
                        )
                        sent_messages.append(msg)
                    case MediaType.ANIMATION:
                        msg = await self.bot.send_animation(
                            **self._common_send_kwargs,
                            animation=input_file,
                            reply_to_message_id=reply_to_id,
                        )
                        sent_messages.append(msg)
                    case MediaType.DOCUMENT:
                        msg = await self.bot.send_document(
                            **self._common_send_kwargs,
                            document=input_file,
                            reply_to_message_id=reply_to_id,
                        )
                        sent_messages.append(msg)
                    case MediaType.STICKER:
                        msg = await self.bot.send_sticker(
                            **self._common_send_kwargs,
                            sticker=input_file,
                            reply_markup=reply_markup or self.reply_markup,
                        )
                        sent_messages.append(msg)
                    case MediaType.VIDEO_NOTE:
                        msg = await self.bot.send_video_note(
                            **self._common_send_kwargs,
                            video_note=input_file,
                            reply_markup=reply_markup or self.reply_markup,
                        )
                        sent_messages.append(msg)

        return sent_messages[-1] if len(sent_messages) == 1 else sent_messages


# -----------------------------------------------------------------------------
# Convenience functions
# -----------------------------------------------------------------------------


async def safe_reply(
    message: Message,
    text: str,
    *,
    reply_markup: ReplyMarkup = None,
) -> Message:
    """Convenience function to safely reply to a message with sanitized text."""
    sender = MessageSender.from_message(message)
    return await sender.reply(text, reply_markup=reply_markup)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    thread_id: int | None = None,
    reply_markup: ReplyMarkup = None,
) -> Message:
    """Convenience function to safely send a message with sanitized text."""
    sender = MessageSender(bot=bot, chat_id=chat_id, thread_id=thread_id)
    return await sender.send(text, reply_markup=reply_markup)
