"""Tests for inline query handler."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from derp.features.inline_chat import (
    InlineChatCompleted,
    InlineChatExhausted,
    InlineChatFailed,
    InlineChatFailureReason,
    InlineChatInvalid,
)
from derp.handlers.inline import (
    chosen_inline_result,
    inline_query_empty,
    inline_query_with_text,
)


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


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

    service = AsyncMock()

    await chosen_inline_result(result, bot, service)

    bot.edit_message_text.assert_not_awaited()
    service.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_chosen_inline_result_success():
    """A typed successful answer is edited without serializing the Telegram user."""
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.from_user.model_dump_json = MagicMock(
        side_effect=AssertionError("profile must not be serialized")
    )
    result.query = "What is Python?"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    service = AsyncMock()
    service.answer.return_value = InlineChatCompleted(
        "Python is a programming language.",
        9,
    )
    user_id = UUID("52ee6f22-6411-41bb-b8e5-1af379ecf508")

    await chosen_inline_result(
        result,
        bot,
        service,
        SimpleNamespace(id=user_id),
    )

    service.answer.assert_awaited_once_with(
        user_id=user_id,
        query="What is Python?",
    )
    result.from_user.model_dump_json.assert_not_called()
    bot.edit_message_text.assert_awaited_once()
    text = _get_text_from_call_args(bot.edit_message_text.call_args)
    assert "Python" in text
    assert "What is Python?" not in text


@pytest.mark.asyncio
async def test_chosen_inline_result_missing_user_model_is_clear():
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.query = "test"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    service = AsyncMock()

    await chosen_inline_result(result, bot, service)

    service.answer.assert_not_awaited()
    text = _get_text_from_call_args(bot.edit_message_text.call_args)
    assert "verify your inline allowance" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            InlineChatExhausted(datetime(2026, 7, 22, tzinfo=UTC), 10),
            "Daily inline limit reached",
        ),
        (InlineChatInvalid(), "empty or too long"),
        (
            InlineChatFailed(
                InlineChatFailureReason.ALLOWANCE_UNAVAILABLE,
                None,
            ),
            "verify your inline allowance",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.PROVIDER_TIMEOUT, 8),
            "took too long",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.PROVIDER_REJECTED, 8),
            "different question",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.UNUSABLE_OUTPUT, 8),
            "no usable answer",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.PROVIDER_ERROR, 8),
            "right now",
        ),
    ],
)
@pytest.mark.asyncio
async def test_chosen_inline_result_renders_every_non_success_state(
    outcome: object,
    expected: str,
) -> None:
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.query = "test"
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    service = AsyncMock()
    service.answer.return_value = outcome

    await chosen_inline_result(
        result,
        bot,
        service,
        SimpleNamespace(id=UUID("52ee6f22-6411-41bb-b8e5-1af379ecf508")),
    )

    bot.edit_message_text.assert_awaited_once()
    assert expected in _get_text_from_call_args(bot.edit_message_text.call_args)


@pytest.mark.asyncio
async def test_chosen_inline_result_exception():
    """Unexpected subsystem failure is redacted and rendered generically."""
    result = MagicMock()
    result.inline_message_id = str(uuid.uuid4())
    result.from_user.id = 12345
    result.query = "test"

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    service = AsyncMock()
    service.answer.side_effect = RuntimeError("private failure detail")

    await chosen_inline_result(
        result,
        bot,
        service,
        SimpleNamespace(id=UUID("52ee6f22-6411-41bb-b8e5-1af379ecf508")),
    )

    bot.edit_message_text.assert_awaited_once()
    text = _get_text_from_call_args(bot.edit_message_text.call_args)
    assert "right now" in text
    assert "private failure detail" not in text
