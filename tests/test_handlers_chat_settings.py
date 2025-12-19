"""Tests for chat settings handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.handlers.chat_settings import (
    cmd_clear_memory,
    cmd_set_memory,
    cmd_show_settings,
)


@pytest.mark.asyncio
async def test_show_settings_with_memory(make_message):
    """Test /settings shows LLM memory when set."""
    message = make_message(text="/settings")

    chat_settings = MagicMock()
    chat_settings.llm_memory = "Remember: user prefers Python"

    await cmd_show_settings(message, chat_settings)

    message.reply.assert_awaited_once()
    response = message.reply.call_args[0][0]
    assert "Chat Settings" in response
    assert "LLM Memory" in response


@pytest.mark.asyncio
async def test_show_settings_without_memory(make_message):
    """Test /settings shows 'Not set' when no memory."""
    message = make_message(text="/settings")

    chat_settings = MagicMock()
    chat_settings.llm_memory = None

    await cmd_show_settings(message, chat_settings)

    message.reply.assert_awaited_once()
    response = message.reply.call_args[0][0]
    assert "Not set" in response


@pytest.mark.asyncio
async def test_show_settings_no_chat(make_message):
    """Test /settings with no chat context."""
    message = make_message(text="/settings")

    await cmd_show_settings(message, None)

    message.reply.assert_awaited_once()
    assert "Not set" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_set_memory_success(make_message, mock_db_client):
    """Test /set_memory updates memory successfully."""
    message = make_message(text="/set_memory User prefers TypeScript")
    message.chat.id = -100123

    with patch(
        "derp.handlers.chat_settings.update_chat_memory", new_callable=AsyncMock
    ) as mock_update:
        await cmd_set_memory(message, mock_db_client)

        mock_update.assert_awaited_once()
        message.answer.assert_awaited_once()
        assert "updated" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_set_memory_no_text(make_message, mock_db_client):
    """Test /set_memory without text shows usage."""
    message = make_message(text="/set_memory")
    message.chat.id = -100123

    await cmd_set_memory(message, mock_db_client)

    message.answer.assert_awaited_once()
    assert "Usage:" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_set_memory_too_long(make_message, mock_db_client):
    """Test /set_memory rejects too-long memory."""
    message = make_message(text="/set_memory " + "x" * 1100)
    message.chat.id = -100123

    await cmd_set_memory(message, mock_db_client)

    message.answer.assert_awaited_once()
    assert "1024 characters" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_set_memory_empty_message_text(make_message, mock_db_client):
    """Test /set_memory with None message text."""
    message = make_message(text=None)
    message.chat.id = -100123

    await cmd_set_memory(message, mock_db_client)

    message.answer.assert_awaited_once()
    assert "Usage:" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_clear_memory_success(make_message, mock_db_client):
    """Test /clear_memory clears memory successfully."""
    message = make_message(text="/clear_memory")
    message.chat.id = -100123

    with patch(
        "derp.handlers.chat_settings.update_chat_memory", new_callable=AsyncMock
    ) as mock_update:
        await cmd_clear_memory(message, mock_db_client)

        mock_update.assert_awaited_once()
        call_args = mock_update.call_args
        assert call_args[1]["llm_memory"] is None

        message.answer.assert_awaited_once()
        assert "cleared" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_clear_memory_error(make_message, mock_db_client):
    """Test /clear_memory handles errors."""
    message = make_message(text="/clear_memory")
    message.chat.id = -100123

    with patch(
        "derp.handlers.chat_settings.update_chat_memory",
        new_callable=AsyncMock,
        side_effect=Exception("DB error"),
    ):
        await cmd_clear_memory(message, mock_db_client)

        message.answer.assert_awaited_once()
        assert "Failed" in message.answer.call_args[0][0]
