"""Tests for video generation handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.catalog import GoogleModelKey
from derp.handlers.video import handle_video


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_text", "arguments", "model_key", "quality"),
    [
        ("a cat", ["a", "cat"], GoogleModelKey.VIDEO_FAST, "fast"),
        (
            "standard a cat",
            ["standard", "a", "cat"],
            GoogleModelKey.VIDEO_STANDARD,
            "standard",
        ),
    ],
)
async def test_handle_video_success(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_db_client,
    mock_credit_service_factory,
    make_credit_check_result,
    target_text,
    arguments,
    model_key,
    quality,
):
    """Test successful video generation flow."""
    message = make_message(text="/video a cat")
    sender = mock_sender(message=message)
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text=target_text, arguments=arguments, command="video")

    check_result = make_credit_check_result(
        allowed=True,
        model_key=model_key,
        source="free",
        free_remaining=1,
    )
    service = mock_credit_service_factory(check_result=check_result)

    with (
        patch(
            "derp.handlers.video.generate_and_send_video", new_callable=AsyncMock
        ) as mock_gen,
        patch("derp.handlers.video.get_db_manager", return_value=mock_db_client),
    ):
        await handle_video(
            message, sender, meta, service, user_model=user, chat_model=chat
        )

        mock_gen.assert_awaited_once()
        service.check_tool_access.assert_awaited_once_with(
            user,
            chat,
            "video_generate",
            arguments={"quality": quality, "duration_seconds": 6},
        )
        assert mock_gen.await_args.kwargs["plan"] is check_result.plan
        assert "quality" not in mock_gen.await_args.kwargs
        assert mock_gen.await_args.kwargs["duration_seconds"] == 6
        assert mock_gen.await_args.kwargs["prompt"] == "a cat"
        service.deduct.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_video_no_credits(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test video generation rejection due to lack of credits."""
    message = make_message(text="/video a cat")
    sender = mock_sender(message=message)
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text="a cat")

    check_result = make_credit_check_result(
        allowed=False,
        model_key=GoogleModelKey.VIDEO_FAST,
        reject_reason="Not enough credits",
    )
    service = mock_credit_service_factory(check_result=check_result)

    with patch(
        "derp.handlers.video.generate_and_send_video", new_callable=AsyncMock
    ) as mock_gen:
        await handle_video(
            message, sender, meta, service, user_model=user, chat_model=chat
        )

        mock_gen.assert_not_awaited()
        sender.reply.assert_awaited_once()
        text = _get_text_from_call_args(sender.reply.call_args)
        assert "Not enough credits" in text


@pytest.mark.asyncio
async def test_handle_video_missing_prompt(
    make_message, mock_sender, mock_meta, mock_credit_service_factory
):
    """Test video generation with missing prompt."""
    message = make_message(text="/video")
    sender = mock_sender(message=message)
    meta = mock_meta(target_text="")
    service = mock_credit_service_factory()

    await handle_video(
        message, sender, meta, service, user_model=MagicMock(), chat_model=MagicMock()
    )

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "Usage: /video" in text
