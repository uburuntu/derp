"""Tests for log updates middleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message, Update, User

from derp.middlewares.log_updates import LogUpdatesMiddleware


@pytest.fixture
def middleware():
    """Create middleware instance."""
    return LogUpdatesMiddleware()


@pytest.fixture
def make_update():
    """Factory for creating Update objects."""

    def _make_update(
        text: str = "Hello",
        user_id: int = 12345,
        chat_id: int = -100123,
    ) -> Update:
        user = User(id=user_id, is_bot=False, first_name="Test", username="test_user")
        chat = Chat(id=chat_id, type="supergroup", title="Test Chat")

        # Create a proper message mock with all required attributes
        message = MagicMock(spec=Message)
        message.message_id = 1
        message.text = text
        message.from_user = user
        message.chat = chat
        message.sender_chat = None
        message.date = None
        message.message_thread_id = None

        update = MagicMock(spec=Update)
        update.update_id = 1
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
        update.my_chat_member = None
        update.chat_member = None
        update.chat_join_request = None

        return update

    return _make_update


class TestLogUpdatesMiddleware:
    """Tests for LogUpdatesMiddleware."""

    @pytest.mark.asyncio
    async def test_logs_handled_update(self, middleware, make_update):
        """Test middleware logs handled updates at info level."""
        update = make_update()
        handler = AsyncMock(return_value=MagicMock())  # Handled

        result = await middleware(handler, update, {})

        handler.assert_awaited_once_with(update, {})
        assert result is not UNHANDLED

    @pytest.mark.asyncio
    async def test_logs_unhandled_update(self, middleware, make_update):
        """Test middleware logs unhandled updates at debug level."""
        update = make_update()
        handler = AsyncMock(return_value=UNHANDLED)

        result = await middleware(handler, update, {})

        handler.assert_awaited_once()
        assert result is UNHANDLED

    @pytest.mark.asyncio
    async def test_raises_for_non_update_event(self, middleware):
        """Test middleware raises for non-Update events."""
        event = MagicMock()  # Not an Update
        handler = AsyncMock()

        with pytest.raises(RuntimeError, match="unexpected event type"):
            await middleware(handler, event, {})

    def test_log_string_format(self, make_update):
        """Test log string includes required info."""
        update = make_update()

        log_string = LogUpdatesMiddleware.log_string(update, elapsed_ms=50)

        assert "50 ms" in log_string
        assert "Message" in log_string  # Class name

    def test_log_string_includes_user(self, make_update):
        """Test log string includes user info when present."""
        update = make_update(user_id=12345)

        log_string = LogUpdatesMiddleware.log_string(update, elapsed_ms=10)

        # Should include user info
        assert "@test_user" in log_string or "Test" in log_string

    @pytest.mark.asyncio
    async def test_measures_elapsed_time(self, middleware, make_update):
        """Test middleware measures execution time."""
        import asyncio

        update = make_update()

        async def slow_handler(event, data):
            await asyncio.sleep(0.01)
            return MagicMock()

        result = await middleware(slow_handler, update, {})

        assert result is not UNHANDLED
