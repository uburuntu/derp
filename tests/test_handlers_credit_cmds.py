"""Tests for credit command handlers."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from derp.billing import CommercePolicy, ProductKind
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseCallback, PurchaseTargetCode
from derp.handlers.credit_cmds import (
    show_buy_chat_options,
    show_buy_options,
    show_credits,
)
from derp.operations import (
    WalletBalance,
    WalletOwner,
    WalletOwnerKind,
    WalletStatement,
)

OPEN_COMMERCE = CommercePolicy(public_intake_enabled=True)


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
) -> WalletStatement:
    return WalletStatement(
        WalletBalance(
            owner=WalletOwner(kind, owner_id),
            allowance_available=allowance,
            purchased_available=purchased,
            reserved=0,
            consumed=0,
            debt=0,
        )
    )


@pytest.fixture
def mock_operation_ledger():
    ledger = MagicMock()
    ledger.statement = AsyncMock()
    ledger.personal_consent_enabled = AsyncMock(return_value=False)
    return ledger


@pytest.mark.asyncio
async def test_show_credits_with_chat(make_message, mock_sender, mock_operation_ledger):
    """Group /credits sends sensitive wallet details only to the actor."""
    message = make_message(text="/credits", chat_type="supergroup")
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.id = UUID(int=1)
    user_model.telegram_id = 12345

    chat_model = MagicMock()
    chat_model.id = UUID(int=2)
    chat_model.telegram_id = -100123
    chat_model.type = "supergroup"
    chat_model.shared_credit_spending_enabled = True
    mock_operation_ledger.statement.side_effect = [
        _balance(WalletOwnerKind.USER, user_model.id, purchased=25),
        _balance(WalletOwnerKind.CHAT, chat_model.id, purchased=50),
    ]

    await show_credits(message, sender, mock_operation_ledger, user_model, chat_model)

    private = message.bot.send_message.await_args.kwargs
    assert private["chat_id"] == user_model.telegram_id
    assert private["protect_content"] is True
    assert "50" in private["text"]
    assert "25" in private["text"]
    assert "This chat" in private["text"]
    public = _get_text_from_call_args(sender.reply.await_args)
    assert public == "I sent your balance in a private chat."
    assert "50" not in public
    assert "25" not in public


@pytest.mark.asyncio
async def test_show_credits_private_chat(
    make_message, mock_sender, mock_operation_ledger
):
    """Test /credits in private chat shows only user credits."""
    message = make_message(text="/credits", chat_type="private", chat_id=12345)
    sender = mock_sender(message=message)

    user_model = MagicMock()
    user_model.id = UUID(int=1)
    user_model.telegram_id = 12345

    chat_model = None  # No chat in private

    mock_operation_ledger.statement.return_value = _balance(
        WalletOwnerKind.USER, user_model.id, allowance=40, purchased=60
    )
    await show_credits(message, sender, mock_operation_ledger, user_model, chat_model)

    response = _get_text_from_call_args(sender.reply.call_args)
    assert "Monthly allowance: 40" in response
    assert "Purchased: 60" in response


@pytest.mark.asyncio
async def test_show_credits_keeps_purchase_controls_out_of_balance_copy(
    make_message, mock_sender, mock_operation_ledger
):
    message = make_message(text="/credits", chat_type="private", chat_id=12345)
    sender = mock_sender(message=message)
    user_model = MagicMock(id=UUID(int=1), telegram_id=12345)
    mock_operation_ledger.statement.return_value = _balance(
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
async def test_show_buy_options_is_fail_closed_by_default(
    make_message,
    mock_sender,
    mock_user_model,
):
    message = make_message(text="/buy")
    sender = mock_sender(message=message)

    await show_buy_options(message, sender, mock_user_model())

    assert "temporarily unavailable" in sender.reply.await_args.args[0]
    assert sender.reply.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_show_buy_options_presents_top_ups_and_personal_plan(
    make_message,
    mock_sender,
    mock_user_model,
):
    message = make_message(text="/buy")
    sender = mock_sender(message=message)
    user = mock_user_model(user_id=UUID(int=1))

    await show_buy_options(message, sender, user, OPEN_COMMERCE)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    response = _get_text_from_call_args(call_args)
    markup = call_args.kwargs["reply_markup"]
    callbacks = [
        PurchaseCallback.unpack(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "your wallet" in response
    assert {callback.product_id for callback in callbacks} == {
        *DEFAULT_PRODUCT_CATALOG.current_top_ups,
        DEFAULT_PRODUCT_CATALOG.subscription_plan.id,
    }
    assert all(callback.target is PurchaseTargetCode.USER for callback in callbacks)
    assert sum(callback.kind is ProductKind.SUBSCRIPTION for callback in callbacks) == 1


@pytest.mark.asyncio
async def test_show_buy_options_no_user(make_message, mock_sender):
    message = make_message(text="/buy")
    sender = mock_sender(message=message)

    await show_buy_options(message, sender, commerce_policy=OPEN_COMMERCE)

    message.reply.assert_awaited_once()
    sender.reply.assert_not_awaited()
    assert "Could not find" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_show_buy_chat_options_is_fail_closed_by_default(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
):
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    await show_buy_chat_options(
        message,
        sender,
        mock_user_model(),
        mock_chat_model(chat_type="supergroup"),
    )

    assert "temporarily unavailable" in sender.reply.await_args.args[0]
    assert sender.reply.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_show_buy_chat_options_in_group_has_shared_top_ups_only(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
):
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)
    user = mock_user_model(user_id=UUID(int=1))
    chat = mock_chat_model(chat_id=UUID(int=2), chat_type="supergroup")

    await show_buy_chat_options(message, sender, user, chat, OPEN_COMMERCE)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    response = _get_text_from_call_args(call_args)
    callbacks = [
        PurchaseCallback.unpack(button.callback_data)
        for row in call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]

    assert "this chat" in response
    assert {callback.product_id for callback in callbacks} == set(
        DEFAULT_PRODUCT_CATALOG.current_top_ups
    )
    assert all(callback.kind is ProductKind.TOP_UP for callback in callbacks)
    assert all(callback.target is PurchaseTargetCode.CHAT for callback in callbacks)


@pytest.mark.asyncio
async def test_show_buy_chat_options_private_falls_back_to_personal_panel(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
):
    message = make_message(text="/buy_chat", chat_type="private")
    sender = mock_sender(message=message)
    user = mock_user_model(user_id=UUID(int=1))
    chat = mock_chat_model(chat_id=UUID(int=2), chat_type="private")

    await show_buy_chat_options(message, sender, user, chat, OPEN_COMMERCE)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    callbacks = [
        PurchaseCallback.unpack(button.callback_data)
        for row in call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "your wallet" in _get_text_from_call_args(call_args)
    assert any(callback.kind is ProductKind.SUBSCRIPTION for callback in callbacks)
    assert all(callback.target is PurchaseTargetCode.USER for callback in callbacks)


@pytest.mark.asyncio
async def test_show_buy_chat_options_no_user(make_message, mock_sender):
    message = make_message(text="/buy_chat")
    sender = mock_sender(message=message)

    await show_buy_chat_options(
        message,
        sender,
        commerce_policy=OPEN_COMMERCE,
    )

    message.reply.assert_awaited_once()
    sender.reply.assert_not_awaited()
    assert "Could not find this chat" in message.reply.await_args.args[0]
