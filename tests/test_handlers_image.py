"""Tests for image generation handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import BinaryImage

from derp.credits.models import ModelTier
from derp.credits.types import CreditCheckResult
from derp.handlers.image import (
    _send_image_result,
    _send_multiple_images,
    _to_filename,
    handle_imagine,
)


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_to_filename_jpeg(self):
        """Test filename generation for JPEG."""
        assert _to_filename("image/jpeg", 1) == "generated_1.jpg"
        assert _to_filename("image/jpg", 2) == "generated_2.jpg"

    def test_to_filename_png(self):
        """Test filename generation for PNG."""
        assert _to_filename("image/png", 1) == "generated_1.png"

    def test_to_filename_other(self):
        """Test filename generation for other types defaults to png."""
        assert _to_filename("image/webp", 1) == "generated_1.png"


class TestSendImageResult:
    """Tests for _send_image_result."""

    @pytest.mark.asyncio
    async def test_sends_text_response(self, make_message):
        """Test sending text response when model returns string."""
        message = make_message(text="/imagine")

        await _send_image_result(message, "Sorry, I can't generate that.")

        message.reply.assert_awaited_once()
        assert "Sorry" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_sends_empty_response(self, make_message):
        """Test sending response for empty string."""
        message = make_message(text="/imagine")

        await _send_image_result(message, "")

        message.reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sends_image_response(self, make_message):
        """Test sending binary image response."""
        message = make_message(text="/imagine")

        image = BinaryImage(data=b"\x89PNG...", media_type="image/png")

        await _send_image_result(message, image)

        message.reply_photo.assert_awaited_once()


class TestSendMultipleImages:
    """Tests for _send_multiple_images."""

    @pytest.mark.asyncio
    async def test_sends_empty_list(self, make_message):
        """Test sending empty list shows error."""
        message = make_message(text="/imagine")

        await _send_multiple_images(message, [])

        message.reply.assert_awaited_once()
        assert "No images" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_sends_single_image(self, make_message):
        """Test single image delegates to _send_image_result."""
        message = make_message(text="/imagine")
        image = BinaryImage(data=b"\x89PNG...", media_type="image/png")

        await _send_multiple_images(message, [image])

        message.reply_photo.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sends_multiple_images_as_group(self, make_message):
        """Test multiple images sent as media group."""
        message = make_message(text="/imagine")

        images = [
            BinaryImage(data=b"\x89PNG1", media_type="image/png"),
            BinaryImage(data=b"\x89PNG2", media_type="image/png"),
        ]

        await _send_multiple_images(message, images)

        message.reply_media_group.assert_awaited_once()


class TestHandleImagine:
    """Tests for /imagine command handler."""

    @pytest.mark.asyncio
    async def test_missing_prompt(self, make_message):
        """Test /imagine without prompt shows usage."""
        message = make_message(text="/imagine")

        meta = MagicMock()
        meta.target_text = ""

        await handle_imagine(message, meta, None, None)

        message.reply.assert_awaited_once()
        assert "Usage" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_user(self, make_message):
        """Test /imagine without user shows error."""
        message = make_message(text="/imagine a cat")

        meta = MagicMock()
        meta.target_text = "a cat"

        await handle_imagine(message, meta, MagicMock(), None)

        message.reply.assert_awaited_once()
        assert "Could not verify" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_credits(self, make_message, mock_db_client):
        """Test /imagine rejected when no credits."""
        message = make_message(text="/imagine a cat")

        meta = MagicMock()
        meta.target_text = "a cat"

        user = MagicMock()
        user.id = "user-uuid"
        user.telegram_id = 12345

        chat = MagicMock()
        chat.id = "chat-uuid"
        chat.telegram_id = -100123

        with (
            patch("derp.handlers.image.get_db_manager", return_value=mock_db_client),
            patch("derp.handlers.image.CreditService") as mock_credit_service,
        ):
            service = mock_credit_service.return_value
            service.check_tool_access = AsyncMock(
                return_value=CreditCheckResult(
                    allowed=False,
                    tier=ModelTier.STANDARD,
                    model_id="gemini-2.5-flash-image",
                    source="rejected",
                    credits_to_deduct=0,
                    credits_remaining=0,
                    free_remaining=0,
                    reject_reason="Not enough credits",
                )
            )

            await handle_imagine(message, meta, chat, user)

            message.reply.assert_awaited_once()
            assert "Not enough credits" in message.reply.call_args[0][0]
