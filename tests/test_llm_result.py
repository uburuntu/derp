"""Typed chat delivery outcomes distinguish model content from error notices."""

from unittest.mock import AsyncMock, MagicMock, patch

from pydantic_ai import BinaryImage

from derp.llm.result import (
    AgentContentDelivered,
    AgentContentUnavailable,
    AgentResult,
)


def test_formatted_text_uses_conversational_section_labels() -> None:
    result = AgentResult(code_blocks=["print('hi')"], execution_results=["hi"])

    assert "**Code:**" in result.formatted_text
    assert "**Result:**" in result.formatted_text
    assert "Generated Code" not in result.formatted_text
    assert "Execution Result" not in result.formatted_text


async def test_rich_send_failure_with_text_fallback_delivers_model_content(
    make_message,
) -> None:
    message = make_message(text="create an image")
    sender = MagicMock()
    builder = MagicMock()
    builder.reply = AsyncMock(side_effect=RuntimeError("rich send failed"))
    sender.compose.return_value = builder
    sender.reply = AsyncMock(return_value=message)
    result = AgentResult(
        text="The requested answer",
        images=[BinaryImage(data=b"image", media_type="image/png")],
    )

    with patch("derp.llm.result.MessageSender.from_message", return_value=sender):
        outcome = await result.reply_to(message)

    assert isinstance(outcome, AgentContentDelivered)
    assert outcome.message_ids == (outcome.message.message_id,)
    assert outcome.message is message
    sender.reply.assert_awaited_once_with("The requested answer")
    message.reply.assert_not_awaited()


async def test_plain_content_send_failure_returns_unavailable_notice(
    make_message,
) -> None:
    message = make_message(text="question")
    sender = MagicMock()
    builder = MagicMock()
    builder.reply = AsyncMock(side_effect=RuntimeError("send failed"))
    sender.compose.return_value = builder
    sender.reply = AsyncMock()
    result = AgentResult(text="The requested answer")

    with patch("derp.llm.result.MessageSender.from_message", return_value=sender):
        outcome = await result.reply_to(message)

    assert isinstance(outcome, AgentContentUnavailable)
    assert outcome.notice is message
    sender.reply.assert_not_awaited()
    assert message.reply.await_args.args[0] == (
        "I couldn't deliver that response. You weren't charged. Try again."
    )
