"""Tests for credit command handlers."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from derp.handlers.credit_cmds import (
    show_buy_chat_options,
    show_buy_options,
    show_credits,
)
from derp.operations import WalletBalance, WalletOwner, WalletOwnerKind


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


def _balance(
    kind: WalletOwnerKind,
    owner_id: UUID,
    *,
    allowance: int = 0,
    purchased: int = 0,
) -> WalletBalance:
    return WalletBalance(
        owner=WalletOwner(kind, owner_id),
        allowance_available=allowance,
        purchased_available=purchased,
        reserved=0,
        consumed=0,
        debt=0,
    )


@pytest.fixture
def mock_operation_ledger():
    ledger = MagicMock()
    ledger.balance = AsyncMock()
    ledger.personal_consent_enabled = AsyncMock(return_value=False)
    return ledger


@pytest.mark.asyncio
async def test_show_credits_with_chat(make_message, mock_sender, mock_operation_ledger):
    """Test /credits shows user and chat credits."""
    message = make_message(text="/credits")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.id = UUID(int=1)
    user_model.telegram_id = 12345

    chat_model = MagicMock()
    chat_model.id = UUID(int=2)
    chat_model.telegram_id = -100123
    chat_model.type = "supergroup"
    chat_model.shared_credit_spending_enabled = True
    mock_operation_ledger.balance.side_effect = [
        _balance(WalletOwnerKind.USER, user_model.id, purchased=25),
        _balance(WalletOwnerKind.CHAT, chat_model.id, purchased=50),
    ]

    await show_credits(message, sender, mock_operation_ledger, user_model, chat_model)

    sender.reply.assert_awaited_once()
    response = _get_text_from_call_args(sender.reply.call_args)
    assert "50" in response
    assert "25" in response
    assert "This chat" in response


@pytest.mark.asyncio
async def test_show_credits_private_chat(
    make_message, mock_sender, mock_operation_ledger
):
    """Test /credits in private chat shows only user credits."""
    message = make_message(text="/credits")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.id = UUID(int=1)
    user_model.telegram_id = 12345

    chat_model = None  # No chat in private

    mock_operation_ledger.balance.return_value = _balance(
        WalletOwnerKind.USER, user_model.id, allowance=40, purchased=60
    )
    await show_credits(message, sender, mock_operation_ledger, user_model, chat_model)

    response = _get_text_from_call_args(sender.reply.call_args)
    assert "Monthly allowance: 40" in response
    assert "Purchased: 60" in response


@pytest.mark.asyncio
async def test_show_credits_does_not_advertise_suspended_purchase(
    make_message, mock_sender, mock_operation_ledger
):
    message = make_message(text="/credits")
    sender = mock_sender(message=message)
    user_model = MagicMock(id=UUID(int=1), telegram_id=12345)
    mock_operation_ledger.balance.return_value = _balance(
        WalletOwnerKind.USER, user_model.id
    )

    await show_credits(message, sender, mock_operation_ledger, user_model, None)

    response = _get_text_from_call_args(sender.reply.call_args)
    assert "/buy" not in response


@pytest.mark.asyncio
async def test_show_credits_no_user(make_message, mock_sender, mock_operation_ledger):
    """Test /credits without user returns error."""
    message = make_message(text="/credits")
    sender = mock_sender(message=message)

    await show_credits(message, sender, mock_operation_ledger, None, None)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "Could not find" in text


@pytest.mark.asyncio
async def test_show_buy_options(make_message, mock_sender):
    """Test /buy rejects without exposing invoice controls."""
    message = make_message(text="/buy")
    sender = mock_sender(message=message)

    await show_buy_options(message, sender)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    response = _get_text_from_call_args(call_args)

    assert "temporarily unavailable" in response
    assert call_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_show_buy_options_no_user(make_message, mock_sender):
    """Test /buy remains fail closed when model context is unavailable."""
    message = make_message(text="/buy")
    sender = mock_sender(message=message)

    await show_buy_options(message, sender)

    sender.reply.assert_awaited_once()
    text = _get_text_from_call_args(sender.reply.call_args)
    assert "temporarily unavailable" in text


@pytest.mark.asyncio
async def test_show_buy_chat_options_in_group(make_message, mock_sender):
    """Test /buy_chat rejects without exposing invoice controls."""
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    await show_buy_chat_options(message, sender)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    response = _get_text_from_call_args(call_args)

    assert "temporarily unavailable" in response
    assert call_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_show_buy_chat_options_private(make_message, mock_sender):
    """Test /buy_chat is suspended in private chats too."""
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    await show_buy_chat_options(message, sender)

    sender.reply.assert_awaited_once()
    text = _get_text_from_call_args(sender.reply.call_args)
    assert "temporarily unavailable" in text


@pytest.mark.asyncio
async def test_show_buy_chat_options_no_user(make_message, mock_sender):
    """Test /buy_chat remains fail closed without model context."""
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    await show_buy_chat_options(message, sender)

    sender.reply.assert_awaited_once()
    text = _get_text_from_call_args(sender.reply.call_args)
    assert "temporarily unavailable" in text
