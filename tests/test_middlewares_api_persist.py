"""Tests for API persist middleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.methods.delete_message import DeleteMessage
from aiogram.methods.send_message import SendMessage
from aiogram.types import Message

from derp.common.update_context import UpdateContext
from derp.middlewares.api_persist import PersistBotActionsMiddleware


@pytest.fixture
def mock_db():
    """Create a mock database manager."""
    return MagicMock()


@pytest.fixture
def middleware(mock_db):
    """Create middleware with mocked db."""
    return PersistBotActionsMiddleware(mock_db)


@pytest.fixture
def mock_bot():
    """Create a mock bot."""
    return MagicMock(spec=Bot)


@pytest.fixture
def mock_context():
    """Create a mock update context."""
    return UpdateContext(
        update_id=1,
        chat_id=-100123,
        user_id=12345,
        thread_id=None,
    )


class TestPersistBotActionsMiddleware:
    """Tests for PersistBotActionsMiddleware."""

    @pytest.mark.asyncio
    async def test_passes_through_without_context(self, middleware, mock_bot):
        """Test middleware passes through when no update context."""
        method = SendMessage(chat_id=-100123, text="Hello")
        expected_result = MagicMock()
        make_request = AsyncMock(return_value=expected_result)

        with patch("derp.middlewares.api_persist.update_ctx") as mock_ctx:
            mock_ctx.get.return_value = None

            result = await middleware(make_request, mock_bot, method)

            assert result is expected_result
            make_request.assert_awaited_once_with(mock_bot, method)

    @pytest.mark.asyncio
    async def test_persists_single_message(self, middleware, mock_bot, mock_context):
        """Test middleware persists single message response."""
        method = SendMessage(chat_id=-100123, text="Hello")
        message_result = MagicMock(spec=Message)
        make_request = AsyncMock(return_value=message_result)

        with (
            patch("derp.middlewares.api_persist.update_ctx") as mock_ctx,
            patch(
                "derp.middlewares.api_persist.upsert_message_from_message",
                new_callable=AsyncMock,
            ) as mock_persist,
        ):
            mock_ctx.get.return_value = mock_context

            result = await middleware(make_request, mock_bot, method)

            mock_persist.assert_awaited_once()
            call_args = mock_persist.call_args
            assert call_args[1]["direction"] == "out"
            assert result is message_result

    @pytest.mark.asyncio
    async def test_persists_multiple_messages(self, middleware, mock_bot, mock_context):
        """Test middleware persists list of messages."""
        method = SendMessage(chat_id=-100123, text="Hello")
        messages = [MagicMock(spec=Message), MagicMock(spec=Message)]
        make_request = AsyncMock(return_value=messages)

        with (
            patch("derp.middlewares.api_persist.update_ctx") as mock_ctx,
            patch(
                "derp.middlewares.api_persist.upsert_message_from_message",
                new_callable=AsyncMock,
            ) as mock_persist,
        ):
            mock_ctx.get.return_value = mock_context

            await middleware(make_request, mock_bot, method)

            # Should persist each message
            assert mock_persist.await_count == 2

    @pytest.mark.asyncio
    async def test_handles_delete_message(self, middleware, mock_bot, mock_context):
        """Test middleware marks messages as deleted."""
        method = DeleteMessage(chat_id=-100123, message_id=456)
        make_request = AsyncMock(return_value=True)

        with (
            patch("derp.middlewares.api_persist.update_ctx") as mock_ctx,
            patch(
                "derp.middlewares.api_persist.mark_deleted",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            mock_ctx.get.return_value = mock_context

            result = await middleware(make_request, mock_bot, method)

            mock_delete.assert_awaited_once()
            call_args = mock_delete.call_args
            assert call_args[1]["chat_id"] == -100123
            assert call_args[1]["message_id"] == 456
            assert result is True

    @pytest.mark.asyncio
    async def test_skips_failed_delete(self, middleware, mock_bot, mock_context):
        """Test middleware skips marking when delete fails."""
        method = DeleteMessage(chat_id=-100123, message_id=456)
        make_request = AsyncMock(return_value=False)

        with (
            patch("derp.middlewares.api_persist.update_ctx") as mock_ctx,
            patch(
                "derp.middlewares.api_persist.mark_deleted",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            mock_ctx.get.return_value = mock_context

            await middleware(make_request, mock_bot, method)

            mock_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_persist_error(self, middleware, mock_bot, mock_context):
        """Test middleware handles persist errors gracefully."""
        method = SendMessage(chat_id=-100123, text="Hello")
        message_result = MagicMock(spec=Message)
        make_request = AsyncMock(return_value=message_result)

        with (
            patch("derp.middlewares.api_persist.update_ctx") as mock_ctx,
            patch(
                "derp.middlewares.api_persist.upsert_message_from_message",
                new_callable=AsyncMock,
                side_effect=Exception("DB error"),
            ),
        ):
            mock_ctx.get.return_value = mock_context

            # Should not raise, just log
            result = await middleware(make_request, mock_bot, method)

            assert result is message_result

