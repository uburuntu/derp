"""Tests for credit command handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.handlers.credit_cmds import (
    show_buy_chat_options,
    show_buy_options,
    show_credits,
)


@pytest.mark.asyncio
async def test_show_credits_with_chat(make_message, mock_db_client):
    """Test /credits shows user and chat credits."""
    message = make_message(text="/credits")

    user = MagicMock()
    user.telegram_id = 12345

    chat = MagicMock()
    chat.telegram_id = -100123
    chat.type = "supergroup"

    with (
        patch("derp.handlers.credit_cmds.get_db_manager", return_value=mock_db_client),
        patch(
            "derp.handlers.credit_cmds.get_balances", new_callable=AsyncMock
        ) as mock_balances,
    ):
        mock_balances.return_value = (50, 25)  # chat_credits, user_credits

        await show_credits(message, chat, user)

        message.reply.assert_awaited_once()
        response = message.reply.call_args[0][0]
        assert "50" in response  # chat credits
        assert "25" in response  # user credits
        assert "Chat pool" in response


@pytest.mark.asyncio
async def test_show_credits_private_chat(make_message, mock_db_client):
    """Test /credits in private chat shows only user credits."""
    message = make_message(text="/credits")

    user = MagicMock()
    user.telegram_id = 12345

    chat = MagicMock()
    chat.telegram_id = 12345
    chat.type = "private"

    with (
        patch("derp.handlers.credit_cmds.get_db_manager", return_value=mock_db_client),
        patch(
            "derp.handlers.credit_cmds.get_balances", new_callable=AsyncMock
        ) as mock_balances,
    ):
        mock_balances.return_value = (0, 100)

        await show_credits(message, chat, user)

        response = message.reply.call_args[0][0]
        assert "Balance" in response
        assert "100" in response


@pytest.mark.asyncio
async def test_show_credits_no_user(make_message):
    """Test /credits without user returns error."""
    message = make_message(text="/credits")

    await show_credits(message, None, None)

    message.reply.assert_awaited_once()
    assert "Could not find" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_show_buy_options(make_message):
    """Test /buy shows credit packs with keyboard."""
    message = make_message(text="/buy")

    user = MagicMock()
    user.telegram_id = 12345

    await show_buy_options(message, None, user)

    message.reply.assert_awaited_once()
    call_args = message.reply.call_args
    response = call_args[0][0]

    assert "Credit Packs" in response
    assert "⭐" in response
    assert call_args[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_show_buy_options_no_user(make_message):
    """Test /buy without user returns error."""
    message = make_message(text="/buy")

    await show_buy_options(message, None, None)

    message.reply.assert_awaited_once()
    assert "Could not find" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_show_buy_chat_options_in_group(make_message):
    """Test /buy_chat in group chat shows chat credit options."""
    message = make_message(text="/buy_chat")

    user = MagicMock()
    user.telegram_id = 12345

    chat = MagicMock()
    chat.telegram_id = -100123
    chat.type = "supergroup"

    await show_buy_chat_options(message, chat, user)

    message.reply.assert_awaited_once()
    call_args = message.reply.call_args
    response = call_args[0][0]

    assert "Chat Credits" in response
    assert call_args[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_show_buy_chat_options_private(make_message):
    """Test /buy_chat in private chat returns error."""
    message = make_message(text="/buy_chat")

    user = MagicMock()
    user.telegram_id = 12345

    chat = MagicMock()
    chat.type = "private"

    await show_buy_chat_options(message, chat, user)

    message.reply.assert_awaited_once()
    assert "group chats only" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_show_buy_chat_options_no_user(make_message):
    """Test /buy_chat without user returns error."""
    message = make_message(text="/buy_chat")

    await show_buy_chat_options(message, None, None)

    message.reply.assert_awaited_once()
    assert "Could not find" in message.reply.call_args[0][0]
