"""Tests for ResilientRequestMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMediaGroup, SendMessage, SendPhoto
from aiogram.types import InputMediaPhoto

from derp.common.sanitize import strip_html_tags
from derp.middlewares.api_resilient import (
    ResilientRequestMiddleware,
    _create_plain_text_method,
)


class TestStripHtmlTags:
    """Tests for strip_html_tags helper (from sanitize module)."""

    def test_strips_bold(self):
        assert strip_html_tags("<b>bold</b>") == "bold"

    def test_strips_italic(self):
        assert strip_html_tags("<i>italic</i>") == "italic"

    def test_strips_multiple_tags(self):
        assert strip_html_tags("<b>bold</b> and <i>italic</i>") == "bold and italic"

    def test_preserves_plain_text(self):
        assert strip_html_tags("plain text") == "plain text"

    def test_handles_empty_string(self):
        assert strip_html_tags("") == ""


class TestCreatePlainTextMethod:
    """Tests for _create_plain_text_method."""

    def test_send_message_strips_html(self):
        method = SendMessage(chat_id=123, text="<b>bold</b> text", parse_mode="HTML")
        plain = _create_plain_text_method(method)

        assert plain is not None
        assert plain.text == "bold text"
        assert plain.parse_mode is None

    def test_send_photo_strips_caption(self):
        method = SendPhoto(
            chat_id=123,
            photo="file_id",
            caption="<b>bold</b> caption",
            parse_mode="HTML",
        )
        plain = _create_plain_text_method(method)

        assert plain is not None
        assert plain.caption == "bold caption"
        assert plain.parse_mode is None

    def test_returns_none_for_unsupported_method(self):
        # Use a method that doesn't have text/caption
        method = MagicMock(spec=[])  # Empty spec means no attributes
        result = _create_plain_text_method(method)
        assert result is None

    def test_returns_none_for_empty_text(self):
        method = SendMessage(chat_id=123, text="", parse_mode="HTML")
        result = _create_plain_text_method(method)
        assert result is None

    def test_send_media_group_strips_captions(self):
        """SendMediaGroup should strip HTML from all media captions."""
        method = SendMediaGroup(
            chat_id=123,
            media=[
                InputMediaPhoto(media="file_id_1", caption="<b>photo 1</b>"),
                InputMediaPhoto(media="file_id_2", caption="<i>photo 2</i>"),
                InputMediaPhoto(media="file_id_3"),  # No caption
            ],
        )
        plain = _create_plain_text_method(method)

        assert plain is not None
        assert plain.media[0].caption == "photo 1"
        assert plain.media[0].parse_mode is None
        assert plain.media[1].caption == "photo 2"
        assert plain.media[1].parse_mode is None
        # Third item should be unchanged (no caption)
        assert plain.media[2].caption is None


class TestResilientMiddleware:
    """Tests for ResilientRequestMiddleware."""

    @pytest.fixture
    def middleware(self):
        return ResilientRequestMiddleware(max_retries=3)

    @pytest.fixture
    def mock_bot(self):
        return MagicMock(spec=Bot)

    @pytest.mark.asyncio
    async def test_passes_through_on_success(self, middleware, mock_bot):
        """Successful requests should pass through unchanged."""
        method = SendMessage(chat_id=123, text="hello")
        make_request = AsyncMock(return_value=MagicMock())

        result = await middleware(make_request, mock_bot, method)

        make_request.assert_awaited_once_with(mock_bot, method)
        assert result is not None

    @pytest.mark.asyncio
    async def test_html_fallback_on_parse_error(self, middleware, mock_bot):
        """Should retry with plain text on HTML parse error."""
        method = SendMessage(chat_id=123, text="<b>bold</b>", parse_mode="HTML")

        make_request = AsyncMock(
            side_effect=[
                TelegramBadRequest(
                    method=MagicMock(),
                    message="Bad Request: can't parse entities",
                ),
                MagicMock(),  # Success on retry
            ]
        )

        result = await middleware(make_request, mock_bot, method)

        assert make_request.await_count == 2
        # Second call should have stripped HTML
        second_call = make_request.call_args_list[1]
        modified_method = second_call.args[1]
        assert modified_method.text == "bold"
        assert modified_method.parse_mode is None
        assert result is not None

    @pytest.mark.asyncio
    async def test_reraises_non_parse_errors(self, middleware, mock_bot):
        """Non-parse errors should be re-raised."""
        method = SendMessage(chat_id=123, text="hello")

        make_request = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: chat not found",
            )
        )

        with pytest.raises(TelegramBadRequest):
            await middleware(make_request, mock_bot, method)

    @pytest.mark.asyncio
    async def test_retry_after_waits_and_retries(self, middleware, mock_bot):
        """Should wait and retry on rate limit errors."""
        method = SendMessage(chat_id=123, text="hello")

        make_request = AsyncMock(
            side_effect=[
                TelegramRetryAfter(
                    method=MagicMock(),
                    message="Flood control",
                    retry_after=0.01,  # 10ms for fast test
                ),
                MagicMock(),  # Success on retry
            ]
        )

        result = await middleware(make_request, mock_bot, method)

        assert make_request.await_count == 2
        assert result is not None

    @pytest.mark.asyncio
    async def test_retry_after_exhausts_max_retries(self, middleware, mock_bot):
        """Should raise after exhausting max retries on rate limit."""
        method = SendMessage(chat_id=123, text="hello")

        # Always return retry after error
        make_request = AsyncMock(
            side_effect=TelegramRetryAfter(
                method=MagicMock(),
                message="Flood control",
                retry_after=0.001,
            )
        )

        with pytest.raises(TelegramRetryAfter):
            await middleware(make_request, mock_bot, method)

        assert make_request.await_count == 3  # max_retries

    @pytest.mark.asyncio
    async def test_html_error_on_unsupported_method_reraises(
        self, middleware, mock_bot
    ):
        """HTML parse error on method without text/caption should re-raise."""
        # Use a mock method with no text or caption
        method = MagicMock(spec=[])

        make_request = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: can't parse entities",
            )
        )

        with pytest.raises(TelegramBadRequest):
            await middleware(make_request, mock_bot, method)

    @pytest.mark.asyncio
    async def test_html_fallback_on_media_group(self, middleware, mock_bot):
        """Should strip HTML from media group captions on parse error."""
        method = SendMediaGroup(
            chat_id=123,
            media=[
                InputMediaPhoto(media="file_id", caption="<b>test</b>"),
            ],
        )

        make_request = AsyncMock(
            side_effect=[
                TelegramBadRequest(
                    method=MagicMock(),
                    message="Bad Request: can't parse entities",
                ),
                MagicMock(),  # Success on retry
            ]
        )

        result = await middleware(make_request, mock_bot, method)

        assert make_request.await_count == 2
        # Second call should have stripped HTML
        second_call = make_request.call_args_list[1]
        modified_method = second_call.args[1]
        assert modified_method.media[0].caption == "test"
        assert result is not None
