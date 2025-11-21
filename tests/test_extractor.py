"""Comprehensive tests for the media and text extractor module.

The Extractor class handles extracting various media types (photos, videos, audio,
documents) and text from Telegram messages, with support for reply policies.
"""

import pytest

from derp.common.extractor import (
    ExtractedAudio,
    ExtractedDocument,
    ExtractedPhoto,
    ExtractedText,
    ExtractedVideo,
    Extractor,
)


class TestExtractorPhoto:
    """Tests for photo extraction functionality."""

    @pytest.mark.asyncio
    async def test_extract_photo_from_message(self, make_message, make_photo):
        """Should extract photo from message.photo."""
        small_photo = make_photo(file_id="small_id", width=320, height=240)
        large_photo = make_photo(file_id="large_id", width=1280, height=960)

        message = make_message()
        message.photo = [small_photo, large_photo]

        result = await Extractor.photo(message)

        assert result is not None
        assert isinstance(result, ExtractedPhoto)
        assert result.file_id == "large_id"
        assert result.width == 1280
        assert result.height == 960

    @pytest.mark.asyncio
    async def test_extract_photo_from_document(self, make_message, make_document):
        """Should extract image documents as photos."""
        doc = make_document(file_id="doc_id", mime_type="image/jpeg", width=800, height=600)

        message = make_message()
        message.document = doc

        result = await Extractor.photo(message)

        assert result is not None
        assert isinstance(result, ExtractedPhoto)
        assert result.file_id == "doc_id"
        assert result.media_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_extract_photo_from_sticker(self, make_message, make_sticker):
        """Should extract static stickers as photos."""
        sticker = make_sticker(file_id="sticker_id", is_animated=False, is_video=False)

        message = make_message()
        message.sticker = sticker

        result = await Extractor.photo(message)

        assert result is not None
        assert isinstance(result, ExtractedPhoto)
        assert result.file_id == "sticker_id"
        assert result.media_type == "image/webp"

    @pytest.mark.asyncio
    async def test_no_photo_returns_none(self, make_message):
        """Should return None if no photo found."""
        message = make_message(text="Just text, no media")

        result = await Extractor.photo(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_photo_prefer_reply_policy(self, make_message, make_photo):
        """Should check reply message first with prefer_reply policy."""
        reply_photo = make_photo(file_id="reply_photo_id", width=800, height=600)
        reply_msg = make_message()
        reply_msg.photo = [reply_photo]

        message = make_message(text="/edit", reply_to_message=reply_msg)

        result = await Extractor.photo(message, reply_policy=Extractor.ReplyPolicy.prefer_reply)

        assert result is not None
        assert result.file_id == "reply_photo_id"


class TestExtractorVideo:
    """Tests for video extraction functionality."""

    @pytest.mark.asyncio
    async def test_extract_video_from_message(self, make_message, make_video):
        """Should extract video from message.video."""
        video = make_video(file_id="video_id", duration=120, width=1920, height=1080)

        message = make_message()
        message.video = video

        result = await Extractor.video(message)

        assert result is not None
        assert isinstance(result, ExtractedVideo)
        assert result.file_id == "video_id"
        assert result.duration == 120
        assert result.width == 1920
        assert result.height == 1080

    @pytest.mark.asyncio
    async def test_extract_video_sticker(self, make_message, make_sticker):
        """Should extract video stickers."""
        sticker = make_sticker(file_id="vsticker_id", is_video=True, duration=3)

        message = make_message()
        message.sticker = sticker

        result = await Extractor.video(message)

        assert result is not None
        assert isinstance(result, ExtractedVideo)
        assert result.file_id == "vsticker_id"
        assert result.media_type == "video/webm"


class TestExtractorAudio:
    """Tests for audio extraction functionality."""

    @pytest.mark.asyncio
    async def test_extract_audio_from_message(self, make_message, make_audio):
        """Should extract audio from message.audio."""
        audio = make_audio(
            file_id="audio_id",
            duration=240,
            title="Test Song",
            performer="Test Artist",
        )

        message = make_message()
        message.audio = audio

        result = await Extractor.audio(message)

        assert result is not None
        assert isinstance(result, ExtractedAudio)
        assert result.file_id == "audio_id"
        assert result.duration == 240
        assert result.title == "Test Song"
        assert result.performer == "Test Artist"


class TestExtractorDocument:
    """Tests for document extraction functionality."""

    @pytest.mark.asyncio
    async def test_extract_document(self, make_message, make_document):
        """Should extract document from message."""
        doc = make_document(
            file_id="doc_id",
            mime_type="application/pdf",
            file_name="report.pdf",
        )

        message = make_message()
        message.document = doc

        result = await Extractor.document(message)

        assert result is not None
        assert isinstance(result, ExtractedDocument)
        assert result.file_id == "doc_id"
        assert result.mime_type == "application/pdf"
        assert result.file_name == "report.pdf"

    @pytest.mark.asyncio
    async def test_no_document_returns_none(self, make_message):
        """Should return None if no document."""
        message = make_message(text="No document here")

        result = await Extractor.document(message)

        assert result is None


class TestExtractorText:
    """Tests for text extraction functionality."""

    @pytest.mark.asyncio
    async def test_extract_text_from_message(self, make_message):
        """Should extract text from message.text."""
        message = make_message(text="Hello, world!")

        result = await Extractor.text(message)

        assert result is not None
        assert isinstance(result, ExtractedText)
        assert result.text == "Hello, world!"
        assert result.length == 13
        assert result.startswith("Hello")
        assert result.contains("world")

    @pytest.mark.asyncio
    async def test_extract_caption_as_text(self, make_message):
        """Should extract caption if no text."""
        message = make_message(text=None, caption="Photo caption")

        result = await Extractor.text(message)

        assert result is not None
        assert result.text == "Photo caption"

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self, make_message):
        """Should return None for messages with no text or caption."""
        message = make_message(text=None, caption=None)

        result = await Extractor.text(message)

        assert result is None


class TestExtractorReplyPolicies:
    """Tests for different reply policies."""

    @pytest.mark.asyncio
    async def test_only_origin_policy(self, make_message):
        """only_origin should only check the original message."""
        reply_msg = make_message(text="Reply text")
        message = make_message(text=None, reply_to_message=reply_msg)

        result = await Extractor.text(message, reply_policy=Extractor.ReplyPolicy.only_origin)

        assert result is None

    @pytest.mark.asyncio
    async def test_prefer_origin_policy(self, make_message):
        """prefer_origin should check origin first, then reply."""
        reply_msg = make_message(text="Reply text")
        message = make_message(text=None, reply_to_message=reply_msg)

        result = await Extractor.text(message, reply_policy=Extractor.ReplyPolicy.prefer_origin)

        assert result is not None
        assert result.text == "Reply text"

    @pytest.mark.asyncio
    async def test_prefer_origin_uses_origin_when_available(self, make_message):
        """prefer_origin should use origin if available."""
        reply_msg = make_message(text="Reply text")
        message = make_message(text="Origin text", reply_to_message=reply_msg)

        result = await Extractor.text(message, reply_policy=Extractor.ReplyPolicy.prefer_origin)

        assert result is not None
        assert result.text == "Origin text"

    @pytest.mark.asyncio
    async def test_prefer_reply_policy(self, make_message):
        """prefer_reply should check reply first."""
        reply_msg = make_message(text="Reply text")
        message = make_message(text="Origin text", reply_to_message=reply_msg)

        result = await Extractor.text(message, reply_policy=Extractor.ReplyPolicy.prefer_reply)

        assert result is not None
        assert result.text == "Reply text"

    @pytest.mark.asyncio
    async def test_only_reply_policy(self, make_message):
        """only_reply should only check the reply message."""
        message = make_message(text="Origin text")
        message.reply_to_message = None

        result = await Extractor.text(message, reply_policy=Extractor.ReplyPolicy.only_reply)

        assert result is None

    @pytest.mark.asyncio
    async def test_only_reply_with_reply_present(self, make_message):
        """only_reply should extract from reply when present."""
        reply_msg = make_message(text="Reply text")
        message = make_message(text="Origin text", reply_to_message=reply_msg)

        result = await Extractor.text(message, reply_policy=Extractor.ReplyPolicy.only_reply)

        assert result is not None
        assert result.text == "Reply text"


class TestExtractorAllMedia:
    """Tests for all_media method that extracts everything."""

    @pytest.mark.asyncio
    async def test_extract_all_media_text_only(self, make_message):
        """Should extract text when no other media present."""
        message = make_message(text="Hello world")

        photo, video, audio, document, text = await Extractor.all_media(message)

        assert photo is None
        assert video is None
        assert audio is None
        assert document is None
        assert text is not None
        assert text.text == "Hello world"

    @pytest.mark.asyncio
    async def test_extract_all_media_with_photo_and_caption(self, make_message, make_photo):
        """Should extract both photo and caption text."""
        photo_obj = make_photo(file_id="photo_id")

        message = make_message(text=None, caption="Photo description")
        message.photo = [photo_obj]

        photo, video, audio, document, text = await Extractor.all_media(message)

        assert photo is not None
        assert photo.file_id == "photo_id"
        assert video is None
        assert audio is None
        assert document is None
        assert text is not None
        assert text.text == "Photo description"


class TestExtractorEdgeCases:
    """Edge case tests for the extractor."""

    @pytest.mark.asyncio
    async def test_image_document_unsupported_format(self, make_message, make_document):
        """Should not extract unsupported image formats."""
        doc = make_document(file_id="doc_id", mime_type="image/svg+xml")

        message = make_message()
        message.document = doc

        result = await Extractor.photo(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_animated_sticker_not_photo(self, make_message, make_sticker):
        """Animated stickers should not be extracted as photos."""
        sticker = make_sticker(file_id="sticker_id", is_animated=True)

        message = make_message()
        message.sticker = sticker

        result = await Extractor.photo(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_text_helper_methods(self, make_message):
        """ExtractedText helper methods should work correctly."""
        message = make_message(text="Test message content")

        result = await Extractor.text(message)

        assert result.startswith("Test") is True
        assert result.startswith("Nope") is False
        assert result.contains("message") is True
        assert result.contains("missing") is False
        assert result.length == 20
