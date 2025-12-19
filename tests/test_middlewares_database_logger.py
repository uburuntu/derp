"""Tests for database logger middleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, Update, User

from derp.middlewares.database_logger import DatabaseLoggerMiddleware


@pytest.fixture
def mock_db():
    """Create a mock database manager."""
    db = MagicMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    db.session.return_value = session
    return db


@pytest.fixture
def middleware(mock_db):
    """Create middleware with mocked db."""
    return DatabaseLoggerMiddleware(mock_db)


@pytest.fixture
def make_update():
    """Factory for creating Update objects."""

    def _make_update(
        user_id: int = 12345,
        chat_id: int = -100123,
        message_text: str = "Hello",
        event_type: str = "message",
    ) -> Update:
        user = User(id=user_id, is_bot=False, first_name="Test", username="test_user")
        chat = Chat(id=chat_id, type="supergroup", title="Test Chat")

        # Create message with all required attributes
        message = MagicMock(spec=Message)
        message.message_id = 1
        message.text = message_text
        message.from_user = user
        message.chat = chat
        message.sender_chat = None
        message.message_thread_id = None
        message.date = None

        update = MagicMock(spec=Update)
        update.update_id = 1
        update.event_type = event_type
        update.message = message
        update.edited_message = None
        update.channel_post = None
        update.edited_channel_post = None
        update.inline_query = None

        return update

    return _make_update


class TestDatabaseLoggerMiddleware:
    """Tests for DatabaseLoggerMiddleware."""

    @pytest.mark.asyncio
    async def test_upserts_user_and_chat(self, middleware, make_update):
        """Test middleware upserts user and chat."""
        update = make_update()
        handler = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "derp.middlewares.database_logger.upsert_user", new_callable=AsyncMock
            ) as mock_user,
            patch(
                "derp.middlewares.database_logger.upsert_chat", new_callable=AsyncMock
            ) as mock_chat,
            patch(
                "derp.middlewares.database_logger.upsert_message_from_update",
                new_callable=AsyncMock,
            ),
        ):
            await middleware(handler, update, {})

            mock_user.assert_awaited_once()
            mock_chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_inline_queries(self, middleware, make_update):
        """Test middleware skips inline queries."""
        update = make_update(event_type="inline_query")
        handler = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "derp.middlewares.database_logger.upsert_user", new_callable=AsyncMock
            ) as mock_user,
            patch(
                "derp.middlewares.database_logger.upsert_chat", new_callable=AsyncMock
            ) as mock_chat,
        ):
            await middleware(handler, update, {})

            mock_user.assert_not_awaited()
            mock_chat.assert_not_awaited()
            handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_calls_handler(self, middleware, make_update):
        """Test middleware calls handler and returns result."""
        update = make_update()
        expected_result = MagicMock()
        handler = AsyncMock(return_value=expected_result)

        with (
            patch(
                "derp.middlewares.database_logger.upsert_user", new_callable=AsyncMock
            ),
            patch(
                "derp.middlewares.database_logger.upsert_chat", new_callable=AsyncMock
            ),
            patch(
                "derp.middlewares.database_logger.upsert_message_from_update",
                new_callable=AsyncMock,
            ),
        ):
            result = await middleware(handler, update, {})

            handler.assert_awaited_once()
            assert result is expected_result

    @pytest.mark.asyncio
    async def test_raises_for_non_update(self, middleware):
        """Test middleware raises for non-Update events."""
        event = MagicMock()  # Not an Update
        handler = AsyncMock()

        with pytest.raises(RuntimeError, match="unexpected event type"):
            await middleware(handler, event, {})

    @pytest.mark.asyncio
    async def test_persists_inbound_message(self, middleware, make_update):
        """Test middleware persists inbound message."""
        update = make_update()
        handler = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "derp.middlewares.database_logger.upsert_user", new_callable=AsyncMock
            ),
            patch(
                "derp.middlewares.database_logger.upsert_chat", new_callable=AsyncMock
            ),
            patch(
                "derp.middlewares.database_logger.upsert_message_from_update",
                new_callable=AsyncMock,
            ) as mock_persist,
        ):
            await middleware(handler, update, {})

            mock_persist.assert_awaited_once()
            call_args = mock_persist.call_args
            assert call_args[1]["direction"] == "in"

    @pytest.mark.asyncio
    async def test_handles_persist_failure(self, middleware, make_update):
        """Test middleware handles message persist failure gracefully."""
        update = make_update()
        handler = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "derp.middlewares.database_logger.upsert_user", new_callable=AsyncMock
            ),
            patch(
                "derp.middlewares.database_logger.upsert_chat", new_callable=AsyncMock
            ),
            patch(
                "derp.middlewares.database_logger.upsert_message_from_update",
                new_callable=AsyncMock,
                side_effect=Exception("DB error"),
            ),
        ):
            # Should not raise, just log warning
            result = await middleware(handler, update, {})

            handler.assert_awaited_once()
            assert result is not None
