"""Tests for derp/common/sender.py - MessageSender class."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from derp.common.sender import (
    MAX_CAPTION_LENGTH,
    MAX_MESSAGE_LENGTH,
    MediaItem,
    MediaType,
    MessageSender,
    _split_text,
    safe_reply,
    safe_send,
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
    async def test_reply_falls_back_on_parse_error(self, mock_message):
        # First call raises parse error, second succeeds
        mock_message.reply.side_effect = [
            TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: can't parse entities",
            ),
            mock_message,
        ]

        sender = MessageSender.from_message(mock_message)
        await sender.reply("test text")

        # Should have been called twice - once with HTML, once without
        assert mock_message.reply.await_count == 2
        second_call = mock_message.reply.call_args_list[1]
        assert second_call.kwargs.get("parse_mode") is None

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
    async def test_edit_falls_back_on_parse_error(self, mock_message):
        mock_message.edit_text.side_effect = [
            TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: can't parse entities",
            ),
            mock_message,
        ]

        sender = MessageSender.from_message(mock_message)
        await sender.edit(mock_message, "test")

        assert mock_message.edit_text.await_count == 2

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

    @pytest.mark.asyncio
    async def test_edit_inline_falls_back_on_parse_error(self, mock_bot):
        mock_bot.edit_message_text.side_effect = [
            TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: can't parse entities",
            ),
            True,
        ]

        sender = MessageSender(bot=mock_bot, chat_id=0)
        await sender.edit_inline("inline_123", "test")

        assert mock_bot.edit_message_text.await_count == 2


class TestMessageSenderPhoto:
    """Tests for photo sending methods."""

    @pytest.mark.asyncio
    async def test_send_photo_with_caption(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        await sender.send_photo(b"image_data", caption="**Nice** photo")

        mock_bot.send_photo.assert_awaited_once()
        call_args = mock_bot.send_photo.call_args
        assert "<b>Nice</b>" in call_args.kwargs.get("caption", "")

    @pytest.mark.asyncio
    async def test_send_photo_with_long_caption(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        long_caption = "x" * (MAX_CAPTION_LENGTH + 100)
        await sender.send_photo(b"image_data", caption=long_caption)

        # Should send photo with truncated caption and follow-up message
        mock_bot.send_photo.assert_awaited_once()
        photo_call = mock_bot.send_photo.call_args
        caption = photo_call.kwargs.get("caption", "")
        assert len(caption) <= MAX_CAPTION_LENGTH

    @pytest.mark.asyncio
    async def test_reply_photo(self, mock_message):
        sender = MessageSender.from_message(mock_message)
        await sender.reply_photo(b"image_data", caption="Caption")

        # reply_photo uses send_photo internally with reply_to
        mock_message.bot.send_photo.assert_awaited_once()


class TestMessageSenderMediaGroup:
    """Tests for media group sending."""

    @pytest.mark.asyncio
    async def test_send_media_group_empty(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        result = await sender.send_media_group([])
        assert result == []
        mock_bot.send_media_group.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_media_group_photos(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        media = [
            MediaItem(type=MediaType.PHOTO, data=b"photo1"),
            MediaItem(type=MediaType.PHOTO, data=b"photo2"),
        ]
        await sender.send_media_group(media, caption="**Album**")

        mock_bot.send_media_group.assert_awaited_once()


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @pytest.mark.asyncio
    async def test_safe_reply(self, mock_message):
        await safe_reply(mock_message, "Hello **world**")
        mock_message.reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_safe_send(self, mock_bot):
        await safe_send(mock_bot, 123, "Hello **world**")
        mock_bot.send_message.assert_awaited_once()


class TestMessageSenderWithMedia:
    """Tests for send_with_media() smart handling."""

    @pytest.mark.asyncio
    async def test_text_only(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        await sender.send_with_media("Just text", [])

        mock_bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_single_photo_with_short_caption(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        media = [MediaItem(type=MediaType.PHOTO, data=b"photo")]
        await sender.send_with_media("Short caption", media)

        # Should send as captioned photo, not separate message + photo
        mock_bot.send_photo.assert_awaited_once()
        mock_bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_error_without_content(self, mock_bot):
        sender = MessageSender(bot=mock_bot, chat_id=123)
        with pytest.raises(ValueError, match="Either text or media must be provided"):
            await sender.send_with_media("", [])
