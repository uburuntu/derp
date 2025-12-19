"""Tests for credit command handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.handlers.credit_cmds import (
    show_buy_chat_options,
    show_buy_options,
    show_credits,
)


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


@pytest.fixture
def mock_credit_service():
    """Create a mock CreditService."""
    service = MagicMock()
    service.session = MagicMock()
    return service


@pytest.mark.asyncio
async def test_show_credits_with_chat(make_message, mock_sender, mock_credit_service):
    """Test /credits shows user and chat credits."""
    message = make_message(text="/credits")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.telegram_id = 12345

    chat_model = MagicMock()
    chat_model.telegram_id = -100123
    chat_model.type = "supergroup"

    with patch(
        "derp.handlers.credit_cmds.get_balances", new_callable=AsyncMock
    ) as mock_balances:
        mock_balances.return_value = (50, 25)  # chat_credits, user_credits

        await show_credits(message, sender, mock_credit_service, user_model, chat_model)

        sender.reply.assert_awaited_once()
        response = _get_text_from_call_args(sender.reply.call_args)
        assert "50" in response  # chat credits
        assert "25" in response  # user credits
        assert "Chat pool" in response


@pytest.mark.asyncio
async def test_show_credits_private_chat(
    make_message, mock_sender, mock_credit_service
):
    """Test /credits in private chat shows only user credits."""
    message = make_message(text="/credits")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.telegram_id = 12345

    chat_model = None  # No chat in private

    with patch(
        "derp.handlers.credit_cmds.get_balances", new_callable=AsyncMock
    ) as mock_balances:
        mock_balances.return_value = (0, 100)

        await show_credits(message, sender, mock_credit_service, user_model, chat_model)

        response = _get_text_from_call_args(sender.reply.call_args)
        assert "Balance" in response
        assert "100" in response


@pytest.mark.asyncio
async def test_show_credits_no_user(make_message, mock_sender, mock_credit_service):
    """Test /credits without user returns error."""
    message = make_message(text="/credits")
    sender = mock_sender(message=message)

    await show_credits(message, sender, mock_credit_service, None, None)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "Could not find" in text


@pytest.mark.asyncio
async def test_show_buy_options(make_message, mock_sender):
    """Test /buy shows credit packs with keyboard."""
    message = make_message(text="/buy")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.telegram_id = 12345

    await show_buy_options(message, sender, user_model, None)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    response = _get_text_from_call_args(call_args)

    assert "Credit Packs" in response
    assert "⭐" in response
    assert call_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_show_buy_options_no_user(make_message, mock_sender):
    """Test /buy without user returns error."""
    message = make_message(text="/buy")
    sender = mock_sender(message=message)

    await show_buy_options(message, sender, None, None)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "Could not find" in text


@pytest.mark.asyncio
async def test_show_buy_chat_options_in_group(make_message, mock_sender):
    """Test /buy_chat in group chat shows chat credit options."""
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.telegram_id = 12345

    chat_model = MagicMock()
    chat_model.telegram_id = -100123
    chat_model.type = "supergroup"

    await show_buy_chat_options(message, sender, user_model, chat_model)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    response = _get_text_from_call_args(call_args)

    assert "Chat Credits" in response
    assert call_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_show_buy_chat_options_private(make_message, mock_sender):
    """Test /buy_chat in private chat returns error."""
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.telegram_id = 12345

    chat_model = MagicMock()
    chat_model.type = "private"

    await show_buy_chat_options(message, sender, user_model, chat_model)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "group chats only" in text


@pytest.mark.asyncio
async def test_show_buy_chat_options_no_user(make_message, mock_sender):
    """Test /buy_chat without user returns error."""
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    await show_buy_chat_options(message, sender, None, None)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "Could not find" in text
