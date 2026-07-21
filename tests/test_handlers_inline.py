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
    assert results[0].title == "Ask Derp"
    assert results[0].description == "Ask a question in this chat."
    assert (
        results[0].input_message_content.message_text
        == "<i>Type a question for Derp.</i>"
    )


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
    assert results[0].title == "Ask Derp"
    assert results[0].description == "Ask Derp: What is Python?"
    assert (
        results[0].input_message_content.message_text
        == "<i>Derp is thinking about: What is Python?</i>"
    )


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
    assert text == "I couldn't verify this request. Open Derp and try again."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            InlineChatExhausted(datetime(2026, 7, 22, tzinfo=UTC), 10),
            "You've used today's inline answers. Try again after 00:00 UTC.",
        ),
        (
            InlineChatInvalid(),
            "That question is empty or too long. Shorten it and try again.",
        ),
        (
            InlineChatFailed(
                InlineChatFailureReason.ALLOWANCE_UNAVAILABLE,
                None,
            ),
            "I couldn't verify this request. Open Derp and try again.",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.PROVIDER_TIMEOUT, 8),
            "That took too long. Try again.",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.PROVIDER_REJECTED, 8),
            "I couldn't answer that question. Try wording it differently.",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.UNUSABLE_OUTPUT, 8),
            "I couldn't produce a useful answer. Try wording it differently.",
        ),
        (
            InlineChatFailed(InlineChatFailureReason.PROVIDER_ERROR, 8),
            "I couldn't answer that here. Try again.",
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
    assert expected == _get_text_from_call_args(bot.edit_message_text.call_args)


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
    assert text == "I couldn't answer that here. Try again."
    assert "private failure detail" not in text
