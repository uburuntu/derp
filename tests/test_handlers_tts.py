"""Tests for TTS handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.catalog import GoogleModelKey, get_google_model
from derp.handlers.tts import handle_tts
from derp.tools.gemini_tts import generate_and_send_tts


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


@pytest.mark.asyncio
async def test_handle_tts_success(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_db_client,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test successful TTS generation flow."""
    message = make_message(text="/tts hello world")
    sender = mock_sender(message=message)
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text="hello world")

    check_result = make_credit_check_result(
        allowed=True,
        model_key=GoogleModelKey.TTS,
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
        await handle_tts(
            message, sender, meta, service, user_model=user, chat_model=chat
        )

        mock_gen.assert_awaited_once()
        service.check_tool_access.assert_awaited_once_with(
            user, chat, "voice_tts", arguments={"text": "hello world"}
        )
        assert mock_gen.await_args.kwargs["model"] is check_result.model
        service.deduct.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_tts_no_credits(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
    mock_meta,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test TTS generation rejection due to lack of credits."""
    message = make_message(text="/tts hello")
    sender = mock_sender(message=message)
    user = mock_user_model()
    chat = mock_chat_model()
    meta = mock_meta(target_text="hello")

    check_result = make_credit_check_result(
        allowed=False,
        model_key=GoogleModelKey.TTS,
        reject_reason="Not enough credits",
    )
    service = mock_credit_service_factory(check_result=check_result)

    with patch(
        "derp.handlers.tts.generate_and_send_tts", new_callable=AsyncMock
    ) as mock_gen:
        await handle_tts(
            message, sender, meta, service, user_model=user, chat_model=chat
        )

        mock_gen.assert_not_awaited()
        sender.reply.assert_awaited_once()
        text = _get_text_from_call_args(sender.reply.call_args)
        assert "Not enough credits" in text


@pytest.mark.asyncio
async def test_tts_provider_output_is_capped_to_the_billed_duration() -> None:
    deps = MagicMock(chat_id=123, message=MagicMock())
    response = MagicMock()
    response.parts = [
        MagicMock(inline_data=MagicMock(data=b"pcm", mime_type="audio/L16"))
    ]
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=response)
    composer = MagicMock()
    composer.voice.return_value = composer
    composer.reply = AsyncMock()
    sender = MagicMock()
    sender.compose.return_value = composer

    with (
        patch("derp.tools.gemini_tts.genai.Client", return_value=client),
        patch(
            "derp.tools.gemini_tts.convert_to_ogg_opus",
            new=AsyncMock(return_value=b"ogg"),
        ),
        patch("derp.tools.gemini_tts.MessageSender.from_message", return_value=sender),
    ):
        await generate_and_send_tts(
            deps,
            text="hello",
            model=get_google_model(GoogleModelKey.TTS),
        )

    config = client.aio.models.generate_content.await_args.kwargs["config"]
    assert config.max_output_tokens == 750
