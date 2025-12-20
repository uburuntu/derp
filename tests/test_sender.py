"""Tests for derp/common/sender.py - MessageSender class."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from derp.common.sender import (
    MAX_MESSAGE_LENGTH,
    ContentBuilder,
    MediaItem,
    MediaType,
    MessageSender,
    _filename_from_mime,
    _split_text,
)


@pytest.fixture
def mock_bot():
    """Create a mock Bot for testing."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_video = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.send_voice = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_sticker = AsyncMock()
    bot.send_video_note = AsyncMock()
    bot.send_animation = AsyncMock()
    bot.send_media_group = AsyncMock(return_value=[MagicMock()])
    bot.edit_message_text = AsyncMock()
    return bot


@pytest.fixture
def mock_message(mock_bot):
    """Create a mock Message for testing."""
    message = MagicMock()
    message.bot = mock_bot
    message.chat = MagicMock()
    message.chat.id = 123456
    message.message_id = 1
    message.message_thread_id = None
    message.business_connection_id = None
    message.reply = AsyncMock(return_value=message)
    message.edit_text = AsyncMock(return_value=message)
    message.reply_photo = AsyncMock(return_value=message)
    return message


class TestSplitText:
    """Tests for _split_text() function."""

    def test_short_text_returns_single_chunk(self):
        text = "Short text"
        result = _split_text(text)
        assert result == ["Short text"]

    def test_empty_text_returns_empty_list(self):
        assert _split_text("") == []
        assert _split_text(None) == []

    def test_splits_at_paragraph_break(self):
        text = "First paragraph.\n\nSecond paragraph."
        # With max_len=30, should split at \n\n
        result = _split_text(text, max_len=30)
        assert len(result) == 2
        assert result[0] == "First paragraph."
        assert result[1] == "Second paragraph."

    def test_splits_at_newline(self):
        text = "First line here.\nSecond line here too."
        result = _split_text(text, max_len=18)
        assert len(result) >= 2
        # Should split at newline when approaching max_len
        assert "First line here." in result[0]

    def test_splits_at_sentence(self):
        text = "First sentence. Second sentence here."
        result = _split_text(text, max_len=25)
        assert len(result) >= 2
        assert "First sentence." in result[0]

    def test_splits_at_space(self):
        text = "word1 word2 word3 word4"
        result = _split_text(text, max_len=12)
        assert len(result) >= 2
        # Should not cut words in half

    def test_hard_cut_when_no_breakpoint(self):
        text = "a" * 100
        result = _split_text(text, max_len=30)
        assert len(result) >= 3
        assert all(len(chunk) <= 30 for chunk in result)


class TestMediaItem:
    """Tests for MediaItem dataclass."""

    def test_to_input_file_from_bytes(self):
        item = MediaItem(type=MediaType.PHOTO, data=b"image_data", filename="test.jpg")
        result = item.to_input_file()
        # Should return BufferedInputFile, not the raw bytes
        assert hasattr(result, "filename")

    def test_to_input_file_from_string(self):
        item = MediaItem(type=MediaType.PHOTO, data="file_id_123")
        result = item.to_input_file()
        assert result == "file_id_123"

    def test_default_filename_photo(self):
        item = MediaItem(type=MediaType.PHOTO, data=b"data")
        result = item.to_input_file()
        assert "jpg" in result.filename

    def test_default_filename_video(self):
        item = MediaItem(type=MediaType.VIDEO, data=b"data")
        result = item.to_input_file()
        assert "mp4" in result.filename


class TestMessageSenderCreation:
    """Tests for MessageSender factory methods."""

    def test_from_message(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        assert sender.bot == mock_message.bot
        assert sender.chat_id == mock_message.chat.id
        assert sender._source_message == mock_message

    def test_direct_creation(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        assert sender.bot == mock_bot
        assert sender.chat_id == 123
        assert sender._source_message is None

    def test_from_message_topic_message_uses_thread_id(self, mock_message):
        """When is_topic_message is True, thread_id should be copied."""
        mock_message.message_thread_id = 42
        mock_message.is_topic_message = True

        sender = MessageSender.from_message(mock_message)

        assert sender.thread_id == 42

    def test_from_message_non_topic_ignores_thread_id(self, mock_message):
        """When is_topic_message is False, thread_id should be None.

        This matches aiogram's behavior where message_thread_id is only passed
        when is_topic_message is True, preventing 'message thread not found' errors.
        """
        mock_message.message_thread_id = 42
        mock_message.is_topic_message = False

        sender = MessageSender.from_message(mock_message)

        assert sender.thread_id is None

    def test_from_message_missing_is_topic_message_ignores_thread_id(
        self, mock_message
    ):
        """When is_topic_message is None (not set), thread_id should be None."""
        mock_message.message_thread_id = 42
        mock_message.is_topic_message = None

        sender = MessageSender.from_message(mock_message)

        assert sender.thread_id is None


class TestMessageSenderSend:
    """Tests for MessageSender.send() method."""

    @pytest.mark.asyncio
    async def test_send_sanitizes_markdown(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        await sender.send("Hello **world**")

        mock_bot.send_message.assert_awaited_once()
        call_args = mock_bot.send_message.call_args
        # Should convert markdown to HTML
        assert "<b>world</b>" in call_args.kwargs.get("text", "")
        assert call_args.kwargs.get("parse_mode") == "HTML"

    @pytest.mark.asyncio
    async def test_send_escapes_special_chars(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        await sender.send("1 < 2 and 3 > 1")

        call_args = mock_bot.send_message.call_args
        text = call_args.kwargs.get("text", "")
        assert "&lt;" in text
        assert "&gt;" in text

    @pytest.mark.asyncio
    async def test_send_chunks_long_text(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        long_text = "x" * (MAX_MESSAGE_LENGTH + 100)
        await sender.send(long_text)

        # Should have been called multiple times
        assert mock_bot.send_message.await_count >= 2


class TestMessageSenderReply:
    """Tests for MessageSender.reply() method."""

    @pytest.mark.asyncio
    async def test_reply_sanitizes_text(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        await sender.reply("Hello **bold**")

        mock_message.reply.assert_awaited_once()
        call_args = mock_message.reply.call_args
        assert "<b>bold</b>" in call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_reply_requires_source_message(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        with pytest.raises(ValueError, match="Cannot reply without source message"):
            await sender.reply("text")


class TestMessageSenderEdit:
    """Tests for MessageSender.edit() method."""

    @pytest.mark.asyncio
    async def test_edit_sanitizes_text(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        await sender.edit(mock_message, "Updated **content**")

        mock_message.edit_text.assert_awaited_once()
        call_args = mock_message.edit_text.call_args
        assert "<b>content</b>" in call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_edit_truncates_long_text(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        long_text = "x" * (MAX_MESSAGE_LENGTH + 100)
        await sender.edit(mock_message, long_text)

        call_args = mock_message.edit_text.call_args
        text = call_args.kwargs.get("text", "")
        assert len(text) <= MAX_MESSAGE_LENGTH
        assert text.endswith("...")


class TestMessageSenderEditInline:
    """Tests for MessageSender.edit_inline() method."""

    @pytest.mark.asyncio
    async def test_edit_inline_sanitizes_text(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=0)
        await sender.edit_inline("inline_123", "**Bold** text")

        mock_bot.edit_message_text.assert_awaited_once()
        call_args = mock_bot.edit_message_text.call_args
        assert "<b>Bold</b>" in call_args.kwargs.get("text", "")
        assert call_args.kwargs.get("inline_message_id") == "inline_123"


class TestFilenameFromMime:
    """Tests for _filename_from_mime() helper."""

    def test_jpeg_image(self):
        assert _filename_from_mime("image/jpeg", 1) == "file_1.jpg"
        assert _filename_from_mime("image/jpg", 2) == "file_2.jpg"

    def test_png_image(self):
        assert _filename_from_mime("image/png", 1) == "file_1.png"

    def test_gif_image(self):
        assert _filename_from_mime("image/gif", 1) == "file_1.gif"

    def test_webp_image(self):
        assert _filename_from_mime("image/webp", 1) == "file_1.webp"

    def test_mp4_video(self):
        assert _filename_from_mime("video/mp4", 1) == "file_1.mp4"

    def test_mp3_audio(self):
        assert _filename_from_mime("audio/mpeg", 1) == "file_1.mp3"
        assert _filename_from_mime("audio/mp3", 2) == "file_2.mp3"

    def test_ogg_audio(self):
        assert _filename_from_mime("audio/ogg", 1) == "file_1.ogg"

    def test_wav_audio(self):
        assert _filename_from_mime("audio/wav", 1) == "file_1.wav"

    def test_custom_prefix(self):
        assert _filename_from_mime("image/jpeg", 1, "photo") == "photo_1.jpg"
        assert _filename_from_mime("video/mp4", 3, "video") == "video_3.mp4"

    def test_unknown_type_defaults_to_bin(self):
        assert _filename_from_mime("application/octet-stream", 1) == "file_1.bin"

    def test_generic_image_defaults_to_jpg(self):
        assert _filename_from_mime("image/unknown", 1) == "file_1.jpg"

    def test_generic_video_defaults_to_mp4(self):
        assert _filename_from_mime("video/unknown", 1) == "file_1.mp4"

    def test_generic_audio_defaults_to_mp3(self):
        assert _filename_from_mime("audio/unknown", 1) == "file_1.mp3"


class TestMediaItemFromBinaryImage:
    """Tests for MediaItem.from_binary_image() class method."""

    def test_creates_photo_media_item(self):
        # Create a mock BinaryImage
        mock_image = MagicMock()
        mock_image.data = b"image_data"
        mock_image.media_type = "image/jpeg"

        item = MediaItem.from_binary_image(mock_image, idx=1)

        assert item.type == MediaType.PHOTO
        assert item.data == b"image_data"
        assert item.mime_type == "image/jpeg"
        assert "image_1.jpg" in item.filename

    def test_creates_png_media_item(self):
        mock_image = MagicMock()
        mock_image.data = b"png_data"
        mock_image.media_type = "image/png"

        item = MediaItem.from_binary_image(mock_image, idx=2)

        assert item.type == MediaType.PHOTO
        assert "image_2.png" in item.filename


class TestContentBuilder:
    """Tests for ContentBuilder fluent API."""

    def test_text_method_returns_self(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()
        result = builder.text("Hello")
        assert result is builder
        assert builder._text == "Hello"

    def test_image_method_with_bytes(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()
        builder.image(b"image_data", mime_type="image/jpeg")

        assert len(builder._images) == 1
        assert builder._images[0].data == b"image_data"
        assert builder._images[0].type == MediaType.PHOTO

    def test_image_method_with_binary_image(self, mock_message):
        mock_img = MagicMock()
        mock_img.data = b"img_data"
        mock_img.media_type = "image/png"

        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()
        builder.image(mock_img)

        assert len(builder._images) == 1
        assert builder._images[0].data == b"img_data"

    def test_images_method_adds_multiple(self, mock_message):
        mock_img1 = MagicMock()
        mock_img1.data = b"img1"
        mock_img1.media_type = "image/jpeg"

        mock_img2 = MagicMock()
        mock_img2.data = b"img2"
        mock_img2.media_type = "image/png"

        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()
        builder.images([mock_img1, mock_img2])

        assert len(builder._images) == 2

    def test_video_method(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()
        builder.video(b"video_data", mime_type="video/mp4")

        assert len(builder._videos) == 1
        assert builder._videos[0].data == b"video_data"
        assert builder._videos[0].type == MediaType.VIDEO

    def test_audio_method(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()
        builder.audio(b"audio_data", mime_type="audio/mpeg")

        assert len(builder._audio) == 1
        assert builder._audio[0].data == b"audio_data"
        assert builder._audio[0].type == MediaType.AUDIO

    def test_fluent_chaining(self, mock_message):
        sender = MessageSender.from_message(mock_message)

        # All methods should return self for chaining
        builder = sender.compose().text("Hello").image(b"image_data")

        assert builder._text == "Hello"
        assert len(builder._images) == 1

    @pytest.mark.asyncio
    async def test_reply_requires_source_message(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        builder = sender.compose().text("Hello")

        with pytest.raises(ValueError, match="Cannot reply without source message"):
            await builder.reply()


class TestMediaGroupTypeSeparation:
    """Tests for separating albums by media type via ContentBuilder."""

    @pytest.mark.asyncio
    async def test_compose_separates_photos_and_videos(self, mock_bot, mock_message):
        """Mixed photos and videos should be sent as separate albums."""
        mock_bot.send_media_group = AsyncMock(return_value=[MagicMock(message_id=1)])

        sender = MessageSender.from_message(mock_message)
        await sender.compose().image(b"photo1").image(b"photo2").video(b"video1").send()

        # Should be called twice: once for photos, once for videos
        assert mock_bot.send_media_group.await_count == 2

    @pytest.mark.asyncio
    async def test_compose_single_type_one_call(self, mock_bot, mock_message):
        """Same-type media should be sent in one call."""
        mock_bot.send_media_group = AsyncMock(return_value=[MagicMock(message_id=1)])

        sender = MessageSender.from_message(mock_message)
        await sender.compose().image(b"photo1").image(b"photo2").image(b"photo3").send()

        # Should be called once for all photos
        assert mock_bot.send_media_group.await_count == 1


class TestComposeMethod:
    """Tests for MessageSender.compose() method."""

    def test_compose_returns_content_builder(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        builder = sender.compose()

        assert isinstance(builder, ContentBuilder)
        assert builder._sender is sender

    @pytest.mark.asyncio
    async def test_compose_send_text_only(self, mock_bot, mock_message):
        sender = MessageSender.from_message(mock_message)

        await sender.compose().text("Hello world").reply()

        # ContentBuilder uses _send_single_message which calls bot.send_message
        mock_bot.send_message.assert_awaited_once()
        call_args = mock_bot.send_message.call_args
        assert "Hello world" in call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_compose_send_single_image(self, mock_bot, mock_message):
        mock_img = MagicMock()
        mock_img.data = b"img_data"
        mock_img.media_type = "image/jpeg"

        sender = MessageSender.from_message(mock_message)
        await sender.compose().image(mock_img).reply()

        # Single image should be sent as photo
        mock_bot.send_photo.assert_awaited_once()
