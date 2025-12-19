"""Tests for deep thinking handler."""

from unittest.mock import AsyncMock, patch

import pytest

from derp.credits.models import ModelTier
from derp.handlers.think import handle_think


@pytest.mark.asyncio
async def test_handle_think_success(
    make_message,
    mock_user_model,
    mock_chat_model,
    mock_db_client,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test successful deep thinking flow."""
    message = make_message(text="/think difficult problem")
    user = mock_user_model()
    chat = mock_chat_model()

    check_result = make_credit_check_result(
        allowed=True,
        tier=ModelTier.PREMIUM,
        model_id="gemini-3-pro-preview",
        credits_to_deduct=10,
        credits_remaining=90,
    )
    service = mock_credit_service_factory(check_result=check_result)

    with (
        patch("derp.handlers.think.create_chat_agent") as mock_create_agent,
        patch("derp.handlers.think.get_db_manager", return_value=mock_db_client),
    ):
        mock_agent = mock_create_agent.return_value
        mock_agent.run = AsyncMock()
        mock_agent.run.return_value.output = "Deep thought result"

        await handle_think(message, service, user_model=user, chat_model=chat)

        mock_create_agent.assert_called_with(ModelTier.PREMIUM)
        mock_agent.run.assert_awaited_once()
        service.deduct.assert_awaited_once()
        message.reply.assert_awaited()
        assert "Deep Thinking Result" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_think_no_credits(
    make_message,
    mock_user_model,
    mock_chat_model,
    mock_credit_service_factory,
    make_credit_check_result,
):
    """Test deep thinking rejection due to lack of credits."""
    message = make_message(text="/think difficult problem")
    user = mock_user_model()
    chat = mock_chat_model()

    check_result = make_credit_check_result(
        allowed=False,
        tier=ModelTier.PREMIUM,
        model_id="gemini-3-pro-preview",
        reject_reason="Not enough credits",
    )
    service = mock_credit_service_factory(check_result=check_result)

    with patch("derp.handlers.think.create_chat_agent") as mock_create_agent:
        await handle_think(message, service, user_model=user, chat_model=chat)

        mock_create_agent.assert_not_called()
        message.reply.assert_awaited()
        assert "Not enough credits" in message.reply.call_args[0][0]
