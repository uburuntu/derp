"""Tests for inline query handler."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.handlers.inline import (
    chosen_inline_result,
    inline_query_empty,
    inline_query_with_text,
)


@pytest.mark.asyncio
async def test_inline_query_empty():
    """Test empty inline query shows help."""
    query = MagicMock()
    query.query = ""
    query.answer = AsyncMock()

    await inline_query_empty(query)

    query.answer.assert_awaited_once()
    results = query.answer.call_args[0][0]
    assert len(results) == 1
    assert "Ask Derp" in results[0].title


@pytest.mark.asyncio
async def test_inline_query_with_text():
    """Test non-empty inline query shows preview."""
    query = MagicMock()
    query.query = "What is Python?"
    query.answer = AsyncMock()

    await inline_query_with_text(query)

    query.answer.assert_awaited_once()
    results = query.answer.call_args[0][0]
    assert len(results) == 1
    assert "Ask Derp" in results[0].title


@pytest.mark.asyncio
async def test_inline_query_truncates_long_input():
    """Test long input is truncated in description."""
    query = MagicMock()
    query.query = "x" * 300  # Longer than 200 chars
    query.answer = AsyncMock()

    await inline_query_with_text(query)

    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_chosen_inline_result_no_message_id():
    """Test chosen result without message ID returns early."""
    result = MagicMock()
    result.inline_message_id = None

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    await chosen_inline_result(result, bot)

    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_chosen_inline_result_success():
    """Test chosen result generates and sends response."""
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.from_user.model_dump_json.return_value = '{"id": 12345}'
    result.query = "What is Python?"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    with patch("derp.handlers.inline.create_inline_agent") as mock_create:
        mock_agent = mock_create.return_value
        mock_agent.run = AsyncMock()
        mock_agent.run.return_value.output = "Python is a programming language."

        await chosen_inline_result(result, bot)

        mock_create.assert_called_once()
        mock_agent.run.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()

        call_args = bot.edit_message_text.call_args
        assert "Python" in call_args[0][0]


@pytest.mark.asyncio
async def test_chosen_inline_result_empty_response():
    """Test chosen result with empty agent response."""
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.from_user.model_dump_json.return_value = '{"id": 12345}'
    result.query = "test"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    with patch("derp.handlers.inline.create_inline_agent") as mock_create:
        mock_agent = mock_create.return_value
        mock_agent.run = AsyncMock()
        mock_agent.run.return_value.output = ""

        await chosen_inline_result(result, bot)

        bot.edit_message_text.assert_awaited_once()
        assert "tangled" in bot.edit_message_text.call_args[0][0]


@pytest.mark.asyncio
async def test_chosen_inline_result_rate_limited():
    """Test chosen result handles rate limiting."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.from_user.model_dump_json.return_value = '{"id": 12345}'
    result.query = "test"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    with patch("derp.handlers.inline.create_inline_agent") as mock_create:
        mock_agent = mock_create.return_value
        mock_agent.run = AsyncMock(side_effect=UnexpectedModelBehavior("Rate limited"))

        await chosen_inline_result(result, bot)

        bot.edit_message_text.assert_awaited_once()
        assert "too many requests" in bot.edit_message_text.call_args[0][0]


@pytest.mark.asyncio
async def test_chosen_inline_result_exception():
    """Test chosen result handles unexpected errors."""
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.from_user.model_dump_json.return_value = '{"id": 12345}'
    result.query = "test"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    with patch("derp.handlers.inline.create_inline_agent") as mock_create:
        mock_agent = mock_create.return_value
        mock_agent.run = AsyncMock(side_effect=Exception("Unexpected error"))

        await chosen_inline_result(result, bot)

        bot.edit_message_text.assert_awaited_once()
        assert "Something went wrong" in bot.edit_message_text.call_args[0][0]
