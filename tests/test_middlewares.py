"""Tests for middleware components."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Update

from derp.middlewares.chat_settings import ChatSettingsMiddleware
from derp.middlewares.event_context import EventContextMiddleware


class TestEventContextMiddleware:
    """Tests for EventContextMiddleware."""

    @pytest.fixture
    def middleware(self, mock_db_client):
        """Create middleware instance with mock database."""
        return EventContextMiddleware(db=mock_db_client)

    @pytest.mark.asyncio
    async def test_injects_bot_and_db(self, middleware, make_message):
        """Should inject bot and db into handler data."""
        message = make_message(text="Test")
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = message
        update.bot = bot

        handler = AsyncMock()
        data = {}

        await middleware(handler, update, data)

        # Should inject bot and db
        assert "bot" in data
        assert data["bot"] == bot
        assert "db" in data
        assert data["db"] == middleware.db

        # Handler should be called
        handler.assert_awaited_once_with(update, data)

    @pytest.mark.asyncio
    async def test_injects_user_when_present(self, middleware, make_message):
        """Should inject user into data when present in event context."""
        message = make_message(text="Test")
        user = message.from_user
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = message
        update.bot = bot

        handler = AsyncMock()
        data = {}

        await middleware(handler, update, data)

        # Should inject user
        assert "user" in data
        assert data["user"] == user

        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_injects_chat_when_present(self, middleware, make_message):
        """Should inject chat into data when present in event context."""
        message = make_message(text="Test", chat_type="supergroup")
        chat = message.chat
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = message
        update.bot = bot

        handler = AsyncMock()
        data = {}

        await middleware(handler, update, data)

        # Should inject chat
        assert "chat" in data
        assert data["chat"] == chat

        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_injects_thread_id_when_present(self, middleware, make_message):
        """Should inject thread_id when message is in a thread."""
        message = make_message(text="Test", message_thread_id=42)
        message.is_topic_message = True  # Must be True for thread_id to be injected
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = message
        update.bot = bot

        handler = AsyncMock()
        data = {}

        await middleware(handler, update, data)

        # Should inject thread_id
        assert "thread_id" in data
        assert data["thread_id"] == 42

        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_on_non_update_event(self, middleware):
        """Should raise RuntimeError for non-Update events."""
        from aiogram.types import Message

        message = MagicMock(spec=Message)
        handler = AsyncMock()
        data = {}

        with pytest.raises(RuntimeError, match="unexpected event type"):
            await middleware(handler, message, data)

    @pytest.mark.asyncio
    async def test_handler_return_value_propagated(self, middleware, make_message):
        """Should return handler's return value."""
        message = make_message(text="Test")
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = message
        update.bot = bot

        handler = AsyncMock(return_value="handler_result")
        data = {}

        result = await middleware(handler, update, data)

        assert result == "handler_result"

    @pytest.mark.asyncio
    async def test_private_chat_message(self, middleware, make_message):
        """Should handle private chat messages correctly."""
        message = make_message(
            text="Private message",
            chat_type="private",
            user_id=12345,
        )
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = message
        update.bot = bot

        handler = AsyncMock()
        data = {}

        await middleware(handler, update, data)

        assert data["user"] == message.from_user
        assert data["chat"] == message.chat
        assert data["bot"] == bot

    @pytest.mark.asyncio
    async def test_edited_message_update(self, middleware, make_message):
        """Should handle edited message updates."""
        message = make_message(text="Edited text")
        bot = message.bot

        update = MagicMock(spec=Update)
        update.message = None
        update.edited_message = message
        update.bot = bot

        handler = AsyncMock()
        data = {}

        await middleware(handler, update, data)

        # Should still inject user and chat from edited message
        assert "user" in data
        assert "chat" in data
        assert "bot" in data

        handler.assert_awaited_once()


class TestChatSettingsMiddleware:
    """Tests for ChatSettingsMiddleware."""

    @pytest.fixture
    def mock_gel_db(self):
        """Create a mock Gel database executor."""
        db = MagicMock()
        executor = AsyncMock()
        db.get_executor.return_value.__aenter__.return_value = executor
        db.get_executor.return_value.__aexit__.return_value = None
        return db, executor

    @pytest.fixture
    def middleware(self, mock_gel_db):
        """Create middleware instance with mock database."""
        db, _ = mock_gel_db
        return ChatSettingsMiddleware(db=db)

    @pytest.mark.asyncio
    async def test_injects_chat_settings_when_chat_present(
        self, middleware, mock_gel_db, make_chat
    ):
        """Should load and inject chat settings when chat is present."""
        from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY

        _, executor = mock_gel_db
        chat = make_chat(id=-100123)

        # Mock the chat_settings query
        mock_settings = {"chat_id": -100123, "some_setting": "value"}

        with patch(
            "derp.middlewares.chat_settings.chat_settings",
            new=AsyncMock(return_value=mock_settings),
        ) as mock_query:
            handler = AsyncMock()
            event = MagicMock()
            data = {EVENT_CHAT_KEY: chat}

            await middleware(handler, event, data)

            # Should call chat_settings query
            mock_query.assert_awaited_once_with(executor, chat_id=chat.id)

            # Should inject chat_settings into data
            assert "chat_settings" in data
            assert data["chat_settings"] == mock_settings

            # Handler should be called
            handler.assert_awaited_once_with(event, data)

    @pytest.mark.asyncio
    async def test_skips_when_no_chat(self, middleware):
        """Should skip processing when no chat in data."""
        handler = AsyncMock()
        event = MagicMock()
        data = {}  # No chat

        await middleware(handler, event, data)

        # Should not inject chat_settings
        assert "chat_settings" not in data

        # Handler should still be called
        handler.assert_awaited_once_with(event, data)

    @pytest.mark.asyncio
    async def test_handles_query_exception_gracefully(
        self, middleware, mock_gel_db, make_chat
    ):
        """Should handle exceptions during chat_settings query."""
        from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY

        chat = make_chat(id=-100123)

        with patch(
            "derp.middlewares.chat_settings.chat_settings",
            new=AsyncMock(side_effect=Exception("Database error")),
        ):
            with patch(
                "derp.middlewares.chat_settings.logfire.exception"
            ) as mock_logfire:
                handler = AsyncMock()
                event = MagicMock()
                data = {EVENT_CHAT_KEY: chat}

                # Should not raise, just log the exception
                await middleware(handler, event, data)

                # Should log the exception
                mock_logfire.assert_called_once()
                args, kwargs = mock_logfire.call_args
                assert "chat_settings_load_failed" in args
                assert kwargs.get("chat_id") == chat.id

                # Should not inject chat_settings
                assert "chat_settings" not in data

                # Handler should still be called
                handler.assert_awaited_once_with(event, data)

    @pytest.mark.asyncio
    async def test_returns_handler_result(self, middleware, mock_gel_db, make_chat):
        """Should return handler's return value."""
        from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY

        _, executor = mock_gel_db
        chat = make_chat(id=-100123)

        with patch(
            "derp.middlewares.chat_settings.chat_settings",
            new=AsyncMock(return_value={}),
        ):
            handler = AsyncMock(return_value="handler_return")
            event = MagicMock()
            data = {EVENT_CHAT_KEY: chat}

            result = await middleware(handler, event, data)

            assert result == "handler_return"

    @pytest.mark.asyncio
    async def test_private_chat_loads_settings(
        self, middleware, mock_gel_db, make_chat
    ):
        """Should load settings for private chats too."""
        from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY

        _, executor = mock_gel_db
        chat = make_chat(id=12345, type="private")

        mock_settings = {"chat_id": 12345, "private": True}

        with patch(
            "derp.middlewares.chat_settings.chat_settings",
            new=AsyncMock(return_value=mock_settings),
        ) as mock_query:
            handler = AsyncMock()
            event = MagicMock()
            data = {EVENT_CHAT_KEY: chat}

            await middleware(handler, event, data)

            # Should load settings even for private chats
            mock_query.assert_awaited_once_with(executor, chat_id=12345)
            assert data["chat_settings"] == mock_settings

    @pytest.mark.asyncio
    async def test_multiple_calls_independent(self, middleware, mock_gel_db, make_chat):
        """Each middleware call should be independent."""
        from aiogram.dispatcher.middlewares.user_context import EVENT_CHAT_KEY

        _, executor = mock_gel_db
        chat1 = make_chat(id=-100111)
        chat2 = make_chat(id=-100222)

        settings1 = {"chat_id": -100111, "setting": "value1"}
        settings2 = {"chat_id": -100222, "setting": "value2"}

        with patch(
            "derp.middlewares.chat_settings.chat_settings",
            new=AsyncMock(side_effect=[settings1, settings2]),
        ) as mock_query:
            handler = AsyncMock()

            # First call
            event1 = MagicMock()
            data1 = {EVENT_CHAT_KEY: chat1}
            await middleware(handler, event1, data1)

            # Second call
            event2 = MagicMock()
            data2 = {EVENT_CHAT_KEY: chat2}
            await middleware(handler, event2, data2)

            # Both should have loaded their own settings
            assert data1["chat_settings"] == settings1
            assert data2["chat_settings"] == settings2
            assert mock_query.await_count == 2
