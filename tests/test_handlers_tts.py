"""Tests for TTS handler."""

from unittest.mock import AsyncMock, patch

import pytest

from derp.credits.models import ModelTier
from derp.handlers.tts import handle_tts


@pytest.mark.asyncio
async def test_handle_tts_success(
    make_message,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_db_client,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test successful TTS generation flow."""
    message = make_message(text="/tts hello world")
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text="hello world")

    check_result = make_credit_check_result(
        allowed=True,
        tier=ModelTier.STANDARD,
        model_id="gemini-2.5-pro-preview-tts",
        source="free",
        free_remaining=1,
    )
    service = mock_credit_service_factory(check_result=check_result)

    with (
        patch(
            "derp.handlers.tts.generate_and_send_tts", new_callable=AsyncMock
        ) as mock_gen,
        patch("derp.handlers.tts.get_db_manager", return_value=mock_db_client),
    ):
        await handle_tts(message, meta, service, user_model=user, chat_model=chat)

        mock_gen.assert_awaited_once()
        service.deduct.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_tts_no_credits(
    make_message,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test TTS generation rejection due to lack of credits."""
    message = make_message(text="/tts hello")
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text="hello")

    check_result = make_credit_check_result(
        allowed=False,
        tier=ModelTier.STANDARD,
        model_id="gemini-2.5-pro-preview-tts",
        reject_reason="Not enough credits",
    )
    service = mock_credit_service_factory(check_result=check_result)

    with patch(
        "derp.handlers.tts.generate_and_send_tts", new_callable=AsyncMock
    ) as mock_gen:
        await handle_tts(message, meta, service, user_model=user, chat_model=chat)

        mock_gen.assert_not_awaited()
        message.reply.assert_awaited_once()
        assert "Not enough credits" in message.reply.call_args[0][0]
