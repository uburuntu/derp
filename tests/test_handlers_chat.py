"""Tests for chat handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import logfire
import pytest
from pydantic_ai import BinaryContent

# Disable logfire instrumentation during tests
logfire.configure(send_to_logfire=False)


class TestExtractMediaForAgent:
    """Tests for extract_media_for_agent function."""

    @pytest.mark.asyncio
    async def test_extracts_photo(self, make_message):
        """Test photo extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_photo = MagicMock()
        mock_photo.download = AsyncMock(return_value=b"\x89PNG...")
        mock_photo.media_type = "image/png"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo_fn,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo_fn.return_value = mock_photo
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert isinstance(result[0], BinaryContent)
            assert result[0].media_type == "image/png"

    @pytest.mark.asyncio
    async def test_extracts_video(self, make_message):
        """Test video extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_video_obj = MagicMock()
        mock_video_obj.download = AsyncMock(return_value=b"video_data")
        mock_video_obj.media_type = "video/mp4"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = mock_video_obj
            mock_audio.return_value = None
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert result[0].media_type == "video/mp4"

    @pytest.mark.asyncio
    async def test_extracts_audio(self, make_message):
        """Test audio extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_audio_obj = MagicMock()
        mock_audio_obj.download = AsyncMock(return_value=b"audio_data")
        mock_audio_obj.media_type = "audio/ogg"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = None
            mock_audio.return_value = mock_audio_obj
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert result[0].media_type == "audio/ogg"

    @pytest.mark.asyncio
    async def test_extracts_pdf_document(self, make_message):
        """Test PDF document extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_doc_obj = MagicMock()
        mock_doc_obj.download = AsyncMock(return_value=b"%PDF...")
        mock_doc_obj.media_type = "application/pdf"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = mock_doc_obj

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert result[0].media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_skips_non_pdf_document(self, make_message):
        """Test non-PDF documents are skipped."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_doc_obj = MagicMock()
        mock_doc_obj.media_type = "application/zip"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = mock_doc_obj

            result = await extract_media_for_agent(message)

            assert len(result) == 0

    @pytest.mark.asyncio
    async def test_handles_download_failure(self, make_message):
        """Test graceful handling of download failures."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_photo = MagicMock()
        mock_photo.download = AsyncMock(side_effect=Exception("Network error"))

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo_fn,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo_fn.return_value = mock_photo
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            # Should not raise, just skip
            assert len(result) == 0


class TestBuildContextPrompt:
    """Tests for build_context_prompt function."""

    @pytest.mark.asyncio
    async def test_includes_chat_info(self, make_message, mock_db_client):
        """Test context includes chat information."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="Hello")
        message.chat.id = -100123
        message.chat.type = "supergroup"
        message.chat.title = "Test Chat"

        with patch(
            "derp.handlers.chat.get_recent_messages", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = []

            result = await build_context_prompt(message, mock_db_client, context_limit=10)

            assert "Test Chat" in result or "supergroup" in result

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Complex mocking required for JSON serialization")
    async def test_includes_recent_messages(self, make_message, mock_db_client):
        """Test context includes recent chat history."""
        # This test requires proper Message model mocks that serialize to JSON
        pass

    @pytest.mark.asyncio
    async def test_includes_current_message(self, make_message, mock_db_client):
        """Test context includes current message text."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="What is Python?")
        message.chat.id = -100123
        message.from_user.username = "asker"

        with patch(
            "derp.handlers.chat.get_recent_messages", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = []

            result = await build_context_prompt(message, mock_db_client, context_limit=10)

            assert "What is Python?" in result

    @pytest.mark.asyncio
    async def test_respects_context_limit(self, make_message, mock_db_client):
        """Test context respects limit parameter."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="Hello")
        message.chat.id = -100123

        with patch(
            "derp.handlers.chat.get_recent_messages", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = []

            await build_context_prompt(message, mock_db_client, context_limit=5)

            mock_get.assert_awaited_once()
            call_args = mock_get.call_args
            assert call_args[1]["limit"] == 5
