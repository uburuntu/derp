"""Tests for Telegram utility functions."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Update

from derp.common.tg import (
    user_info,
    chat_info,
    message_info,
    decompose_update,
    extract_attachment_info,
    extract_attachment_file_id,
)


class TestUserInfo:
    """Tests for user_info function."""

    def test_user_with_all_fields(self, make_user):
        """Should format user with all fields present."""
        user = make_user(
            id=12345,
            first_name="John",
            last_name="Doe",
            username="johndoe",
            language_code="en",
        )
        result = user_info(user)
        assert result == "John Doe (12345, @johndoe, en)"

    def test_user_without_last_name(self, make_user):
        """Should handle user without last name."""
        user = make_user(
            id=12345,
            first_name="John",
            last_name=None,
            username="johndoe",
            language_code="en",
        )
        result = user_info(user)
        assert result == "John (12345, @johndoe, en)"

    def test_user_without_username(self, make_user):
        """Should handle user without username."""
        user = make_user(
            id=12345,
            first_name="John",
            last_name="Doe",
            username=None,
            language_code="en",
        )
        result = user_info(user)
        assert result == "John Doe (12345, en)"

    def test_user_without_language_code(self, make_user):
        """Should handle user without language code."""
        user = make_user(
            id=12345,
            first_name="John",
            last_name="Doe",
            username="johndoe",
            language_code=None,
        )
        result = user_info(user)
        assert result == "John Doe (12345, @johndoe)"

    def test_user_minimal_fields(self, make_user):
        """Should handle user with only required fields."""
        user = make_user(
            id=12345,
            first_name="John",
            last_name=None,
            username=None,
            language_code=None,
        )
        result = user_info(user)
        assert result == "John (12345)"

    def test_user_with_sender_chat(self, make_user, make_chat):
        """When sender_chat is provided, should return chat info instead."""
        user = make_user(id=12345, first_name="John")
        sender_chat = make_chat(id=-100123, type="channel", title="News Channel")

        result = user_info(user, sender_chat=sender_chat)
        assert "channel" in result
        assert "News Channel" in result
        assert "-100123" in result


class TestChatInfo:
    """Tests for chat_info function."""

    def test_private_chat(self, make_chat):
        """Private chats should return 'private'."""
        chat = make_chat(type="private")
        result = chat_info(chat)
        assert result == "private"

    def test_group_chat_with_username(self, make_chat):
        """Should format group chat with username."""
        chat = make_chat(
            id=-1001234567890,
            type="supergroup",
            title="Test Group",
            username="testgroup",
        )
        result = chat_info(chat)
        assert result == "supergroup | Test Group (-1001234567890, @testgroup)"

    def test_group_chat_without_username(self, make_chat):
        """Should format group chat without username."""
        chat = make_chat(
            id=-1001234567890,
            type="supergroup",
            title="Test Group",
            username=None,
        )
        result = chat_info(chat)
        assert result == "supergroup | Test Group (-1001234567890)"

    def test_channel_chat(self, make_chat):
        """Should handle channel type."""
        chat = make_chat(
            id=-1001234567890,
            type="channel",
            title="News Channel",
            username="news",
        )
        result = chat_info(chat)
        assert result == "channel | News Channel (-1001234567890, @news)"

    def test_group_chat(self, make_chat):
        """Should handle regular group type."""
        chat = make_chat(
            id=-123456,
            type="group",
            title="Small Group",
        )
        result = chat_info(chat)
        assert "group" in result
        assert "Small Group" in result


class TestMessageInfo:
    """Tests for message_info function."""

    def test_message_with_text(self, make_message):
        """Should include message text."""
        message = make_message(message_id=42, text="Hello, world!")
        result = message_info(message)
        assert result == "42 | Hello, world!"

    def test_message_with_long_text(self, make_message):
        """Should truncate long text to 50 characters."""
        long_text = "A" * 100
        message = make_message(message_id=42, text=long_text)
        result = message_info(message)
        assert result == f"42 | {'A' * 50}"
        assert len(result) == 55  # "42 | " + 50 chars

    def test_message_with_newlines(self, make_message):
        """Should convert multiline text to single line."""
        message = make_message(message_id=42, text="Line 1\nLine 2\nLine 3")
        result = message_info(message)
        assert result == "42 | Line 1 Line 2 Line 3"

    def test_message_without_text(self, make_message, make_photo):
        """Should show content type when no text."""
        message = make_message(message_id=42, text=None, content_type="photo")
        result = message_info(message)
        assert result == "42 | type: photo"

    def test_message_with_video(self, make_message):
        """Should show content type for video."""
        message = make_message(message_id=42, text=None, content_type="video")
        result = message_info(message)
        assert result == "42 | type: video"


class TestExtractAttachmentInfo:
    """Tests for extract_attachment_info function."""

    def test_extract_photo(self, make_message, make_photo):
        """Should extract photo information."""
        photo = make_photo(file_id="photo123")
        message = make_message()
        message.photo = [photo]

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "photo"
        assert file_id == "photo123"
        assert filename is None

    def test_extract_audio(self, make_message, make_audio):
        """Should extract audio information."""
        audio = make_audio(file_id="audio123")
        audio.file_name = "song.mp3"
        message = make_message()
        message.audio = audio

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "audio"
        assert file_id == "audio123"
        assert filename == "song.mp3"

    def test_extract_voice(self, make_message):
        """Should extract voice message information."""
        from aiogram.types import Voice
        voice = MagicMock(spec=Voice)
        voice.file_id = "voice123"

        message = make_message()
        message.voice = voice

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "voice"
        assert file_id == "voice123"
        assert filename is None

    def test_extract_sticker(self, make_message, make_sticker):
        """Should extract sticker information."""
        sticker = make_sticker(file_id="sticker123")
        message = make_message()
        message.sticker = sticker

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "sticker"
        assert file_id == "sticker123"
        assert filename is None

    def test_extract_video(self, make_message, make_video):
        """Should extract video information."""
        video = make_video(file_id="video123")
        video.file_name = "clip.mp4"
        message = make_message()
        message.video = video

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "video"
        assert file_id == "video123"
        assert filename == "clip.mp4"

    def test_extract_video_note(self, make_message):
        """Should extract video note information."""
        from aiogram.types import VideoNote
        video_note = MagicMock(spec=VideoNote)
        video_note.file_id = "videonote123"

        message = make_message()
        message.video_note = video_note

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "video_note"
        assert file_id == "videonote123"
        assert filename is None

    def test_extract_animation(self, make_message):
        """Should extract animation (GIF) information."""
        from aiogram.types import Animation
        animation = MagicMock(spec=Animation)
        animation.file_id = "anim123"
        animation.file_name = "funny.gif"

        message = make_message()
        message.animation = animation

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "animation"
        assert file_id == "anim123"
        assert filename == "funny.gif"

    def test_extract_document(self, make_message, make_document):
        """Should extract document information."""
        doc = make_document(file_id="doc123", file_name="report.pdf")
        message = make_message()
        message.document = doc

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ == "document"
        assert file_id == "doc123"
        assert filename == "report.pdf"

    def test_no_attachment(self, make_message):
        """Should return None values when no attachment."""
        message = make_message(text="Just text")

        type_, file_id, filename = extract_attachment_info(message)

        assert type_ is None
        assert file_id is None
        assert filename is None

    def test_priority_order_photo_first(self, make_message, make_photo, make_document):
        """Photo should take priority over document."""
        photo = make_photo(file_id="photo123")
        doc = make_document(file_id="doc123")

        message = make_message()
        message.photo = [photo]
        message.document = doc

        type_, file_id, _ = extract_attachment_info(message)

        # Should extract photo, not document
        assert type_ == "photo"
        assert file_id == "photo123"


class TestExtractAttachmentFileId:
    """Tests for extract_attachment_file_id convenience function."""

    def test_extracts_file_id(self, make_message, make_photo):
        """Should extract just the file_id."""
        photo = make_photo(file_id="photo123")
        message = make_message()
        message.photo = [photo]

        file_id = extract_attachment_file_id(message)

        assert file_id == "photo123"

    def test_returns_none_when_no_attachment(self, make_message):
        """Should return None when no attachment."""
        message = make_message(text="No attachment")

        file_id = extract_attachment_file_id(message)

        assert file_id is None


class TestDecomposeUpdate:
    """Tests for decompose_update function."""

    def test_decompose_message_update(self, make_message):
        """Should decompose regular message update."""
        message = make_message(text="Hello!")
        message.sender_chat = None

        update = MagicMock(spec=Update)
        update.message = message
        update.edited_message = None
        update.channel_post = None
        update.edited_channel_post = None
        update.inline_query = None
        update.chosen_inline_result = None
        update.callback_query = None
        update.shipping_query = None
        update.pre_checkout_query = None
        update.poll = None
        update.poll_answer = None
        update.chat_member = None
        update.my_chat_member = None

        obj, user, sender_chat, chat, info = decompose_update(update)

        assert obj == message
        assert user == message.from_user
        assert sender_chat is None
        assert chat == message.chat
        assert "Hello!" in info

    def test_decompose_edited_message(self, make_message):
        """Should decompose edited message update."""
        message = make_message(text="Edited text")
        message.sender_chat = None

        update = MagicMock(spec=Update)
        update.message = None
        update.edited_message = message
        update.channel_post = None
        update.edited_channel_post = None
        update.inline_query = None
        update.chosen_inline_result = None
        update.callback_query = None
        update.shipping_query = None
        update.pre_checkout_query = None
        update.poll = None
        update.poll_answer = None
        update.chat_member = None
        update.my_chat_member = None

        obj, user, sender_chat, chat, info = decompose_update(update)

        assert obj == message
        assert "[edited]" in info

    def test_decompose_inline_query(self):
        """Should decompose inline query update."""
        from aiogram.types import InlineQuery, User

        inline_query = MagicMock(spec=InlineQuery)
        inline_query.from_user = MagicMock(spec=User)
        inline_query.query = "search query"

        update = MagicMock(spec=Update)
        update.message = None
        update.edited_message = None
        update.channel_post = None
        update.edited_channel_post = None
        update.inline_query = inline_query
        update.chosen_inline_result = None
        update.callback_query = None
        update.shipping_query = None
        update.pre_checkout_query = None
        update.poll = None
        update.poll_answer = None
        update.chat_member = None
        update.my_chat_member = None

        obj, user, sender_chat, chat, info = decompose_update(update)

        assert obj == inline_query
        assert user == inline_query.from_user
        assert "search query" in info

    def test_decompose_callback_query_with_message(self, make_message):
        """Should decompose callback query with message."""
        from aiogram.types import CallbackQuery

        message = make_message(text="Button message")
        callback = MagicMock(spec=CallbackQuery)
        callback.message = message
        callback.from_user = message.from_user
        callback.data = "button_data"

        update = MagicMock(spec=Update)
        update.message = None
        update.edited_message = None
        update.channel_post = None
        update.edited_channel_post = None
        update.inline_query = None
        update.chosen_inline_result = None
        update.callback_query = callback
        update.shipping_query = None
        update.pre_checkout_query = None
        update.poll = None
        update.poll_answer = None
        update.chat_member = None
        update.my_chat_member = None

        obj, user, sender_chat, chat, info = decompose_update(update)

        assert obj == callback
        assert user == callback.from_user
        assert chat == message.chat
        assert info == "button_data"

    def test_decompose_callback_query_without_message(self):
        """Should decompose callback query without message."""
        from aiogram.types import CallbackQuery, User

        callback = MagicMock(spec=CallbackQuery)
        callback.message = None
        callback.from_user = MagicMock(spec=User)
        callback.data = "inline_button"

        update = MagicMock(spec=Update)
        update.message = None
        update.edited_message = None
        update.channel_post = None
        update.edited_channel_post = None
        update.inline_query = None
        update.chosen_inline_result = None
        update.callback_query = callback
        update.shipping_query = None
        update.pre_checkout_query = None
        update.poll = None
        update.poll_answer = None
        update.chat_member = None
        update.my_chat_member = None

        obj, user, sender_chat, chat, info = decompose_update(update)

        assert obj == callback
        assert user == callback.from_user
        assert chat is None
        assert info == "inline_button"


class TestEdgeCases:
    """Edge case tests for tg utilities."""

    def test_user_info_with_unicode(self, make_user):
        """Should handle unicode characters in names."""
        user = make_user(
            id=12345,
            first_name="Иван",
            last_name="Иванов",
            username="ivan",
        )
        result = user_info(user)
        assert "Иван Иванов" in result

    def test_chat_info_with_unicode_title(self, make_chat):
        """Should handle unicode in chat titles."""
        chat = make_chat(
            id=-100123,
            type="supergroup",
            title="Русский чат",
        )
        result = chat_info(chat)
        assert "Русский чат" in result

    def test_message_info_empty_text(self, make_message):
        """Should handle empty text."""
        message = make_message(message_id=42, text="")
        result = message_info(message)
        # Empty string is falsy, so shows content_type
        assert result == "42 | type: text"

    def test_extract_attachment_photo_multiple_sizes(self, make_message, make_photo):
        """Should extract largest photo from array."""
        small = make_photo(file_id="small", width=320)
        large = make_photo(file_id="large", width=1280)

        message = make_message()
        message.photo = [small, large]

        type_, file_id, _ = extract_attachment_info(message)

        assert type_ == "photo"
        # Should get the last one (largest)
        assert file_id == "large"
