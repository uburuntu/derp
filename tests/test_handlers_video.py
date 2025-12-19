"""Tests for video generation handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.credits.models import ModelTier
from derp.handlers.video import handle_video


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


@pytest.mark.asyncio
async def test_handle_video_success(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_db_client,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test successful video generation flow."""
    message = make_message(text="/video a cat")
    sender = mock_sender(message=message)
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text="a cat")

    check_result = make_credit_check_result(
        allowed=True,
        tier=ModelTier.STANDARD,
        model_id="veo-3.1-fast-generate-preview",
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
        tier=ModelTier.STANDARD,
        model_id="veo-3.1-fast-generate-preview",
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
