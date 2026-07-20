"""Tests for admin diagnostics and the durable one-Star purchase path."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiogram.types import CallbackQuery

from derp.billing import PurchaseIntentHandle, PurchaseTarget
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseTargetCode
from derp.handlers.debug import (
    DebugPurchaseCallback,
    debug_add_credits,
    debug_buy_command,
    debug_help,
    debug_refund,
    debug_status,
    debug_tools,
    handle_debug_buy_callback,
    reject_legacy_debug_buy_callback,
)

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _debug_handle(target: PurchaseTarget) -> PurchaseIntentHandle:
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up
    return PurchaseIntentHandle(
        intent_id=UUID(int=10),
        invoice_payload="dpi1_debug-opaque-token",
        product_kind=product.kind,
        product_id=product.id,
        product_version=product.version,
        target=target,
        credits=product.credits,
        stars=product.stars,
        currency=product.currency,
        expires_at=NOW,
        subscription_period_seconds=None,
    )


def _purchase_intents(handle: PurchaseIntentHandle | None = None) -> MagicMock:
    service = MagicMock()
    service.create_admin_debug_top_up_intent = AsyncMock(return_value=handle)
    return service


def _callback(make_message, make_user) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = make_message(text="debug purchase")
    callback.message.business_connection_id = "business-1"
    callback.from_user = make_user(id=12345)
    callback.bot = MagicMock()
    callback.bot.create_invoice_link = AsyncMock(
        return_value="https://t.me/$debug-invoice"
    )
    callback.answer = AsyncMock()
    return callback


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


@pytest.mark.asyncio
async def test_debug_buy_command_shows_only_personal_target_in_private_chat(
    make_message,
    mock_sender,
    mock_chat_model,
):
    message = make_message(text="/debug_buy")
    sender = mock_sender(message=message)
    chat = mock_chat_model(telegram_id=message.chat.id, chat_type="private")

    await debug_buy_command(message, sender, chat)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    text = _get_text_from_call_args(call_args)
    assert "1 Star" in text
    buttons = call_args.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    callback = DebugPurchaseCallback.unpack(buttons[0][0].callback_data)
    assert callback.product_id == DEFAULT_PRODUCT_CATALOG.debug_top_up.id
    assert callback.target is PurchaseTargetCode.USER


@pytest.mark.asyncio
async def test_debug_buy_command_includes_current_shared_chat(
    make_message,
    mock_sender,
    mock_chat_model,
):
    message = make_message(text="/debug_buy")
    sender = mock_sender(message=message)
    chat = mock_chat_model(telegram_id=message.chat.id, chat_type="supergroup")

    await debug_buy_command(message, sender, chat)

    buttons = sender.reply.await_args.kwargs["reply_markup"].inline_keyboard
    callbacks = [DebugPurchaseCallback.unpack(row[0].callback_data) for row in buttons]
    assert [callback.target for callback in callbacks] == [
        PurchaseTargetCode.USER,
        PurchaseTargetCode.CHAT,
    ]


@pytest.mark.asyncio
async def test_debug_add_credits(
    make_message, mock_sender, mock_user_model, mock_credit_service_factory
):
    """Test /debug_credits command adds credits."""
    message = make_message(text="/debug_credits 50")
    sender = mock_sender(message=message)

    user = mock_user_model(telegram_id=12345)
    service = mock_credit_service_factory(purchase_result=50)

    await debug_add_credits(message, sender, service, user, None)

    service.purchase_credits.assert_awaited_once()
    sender.reply.assert_awaited_once()
    text = _get_text_from_call_args(sender.reply.call_args)
    # Sender.reply takes text as first positional arg
    assert "Added" in text and "50" in text and "credits" in text


@pytest.mark.asyncio
async def test_debug_add_credits_no_user(
    make_message, mock_sender, mock_credit_service_factory
):
    """Test /debug_credits without user returns error."""
    message = make_message(text="/debug_credits 50")
    sender = mock_sender(message=message)
    service = mock_credit_service_factory()

    await debug_add_credits(message, sender, service, None, None)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "User not found" in text


@pytest.mark.asyncio
async def test_debug_status(
    make_message,
    mock_sender,
    mock_user_model,
    mock_chat_model,
    mock_credit_service_factory,
):
    """Test /debug_status shows diagnostics."""
    message = make_message(text="/debug_status")
    sender = mock_sender(message=message)

    user = mock_user_model(telegram_id=12345)
    chat = mock_chat_model(telegram_id=-100123)
    chat.admin_policy = "Test policy"

    service = mock_credit_service_factory()

    service.get_balances.return_value = (100, 50)

    await debug_status(message, sender, service, user, chat)

    service.get_balances.assert_awaited_once_with(12345, -100123)
    sender.reply.assert_awaited_once()
    response = _get_text_from_call_args(sender.reply.call_args)
    assert "Debug Status" in response
    assert "12345" in response  # user telegram id


@pytest.mark.asyncio
async def test_debug_status_no_user(
    make_message, mock_sender, mock_credit_service_factory
):
    """Test /debug_status without user returns error."""
    message = make_message(text="/debug_status")
    sender = mock_sender(message=message)
    service = mock_credit_service_factory()

    await debug_status(message, sender, service, None, None)

    message.reply.assert_awaited_once()
    text = _get_text_from_call_args(message.reply.call_args)
    assert "User not found" in text


@pytest.mark.asyncio
async def test_debug_refund(make_message, mock_sender, mock_credit_service_factory):
    """Test /debug_refund processes refund."""
    message = make_message(text="/debug_refund charge_123")
    sender = mock_sender(message=message)
    service = mock_credit_service_factory()
    service.refund_credits = AsyncMock(return_value=True)

    await debug_refund(message, sender, service)

    service.refund_credits.assert_awaited_once_with("charge_123")
    sender.reply.assert_awaited_once()
    text = _get_text_from_call_args(sender.reply.call_args)
    assert "Refund processed" in text


@pytest.mark.asyncio
async def test_debug_tools(make_message, mock_sender):
    """Test /debug_tools lists available tools."""
    message = make_message(text="/debug_tools")
    sender = mock_sender(message=message)

    await debug_tools(message, sender)

    sender.reply.assert_awaited_once()
    response = _get_text_from_call_args(sender.reply.call_args)
    assert "Available Tools" in response


@pytest.mark.asyncio
async def test_debug_help(make_message, mock_sender):
    """Test /debug_help shows all commands."""
    message = make_message(text="/debug_help")
    sender = mock_sender(message=message)

    await debug_help(message, sender)

    sender.reply.assert_awaited_once()
    response = _get_text_from_call_args(sender.reply.call_args)
    assert "Debug Commands" in response
    assert "/debug_buy" in response
    assert "/debug_credits" in response


@pytest.mark.asyncio
async def test_debug_personal_callback_creates_exact_durable_invoice(
    make_message,
    make_user,
    mock_user_model,
):
    user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
    handle = _debug_handle(PurchaseTarget.user(user.id))
    service = _purchase_intents(handle)
    callback = _callback(make_message, make_user)

    await handle_debug_buy_callback(
        callback,
        DebugPurchaseCallback(
            product_id=DEFAULT_PRODUCT_CATALOG.debug_top_up.id,
            target=PurchaseTargetCode.USER,
        ),
        service,
        user,
    )

    service.create_admin_debug_top_up_intent.assert_awaited_once_with(
        payer_user_id=user.id,
        target=PurchaseTarget.user(user.id),
    )
    invoice = callback.bot.create_invoice_link.await_args.kwargs
    assert invoice["payload"] == handle.invoice_payload
    assert invoice["currency"] == "XTR"
    assert invoice["provider_token"] == ""
    assert invoice["subscription_period"] is None
    assert invoice["business_connection_id"] == "business-1"
    assert len(invoice["prices"]) == 1
    assert invoice["prices"][0].amount == 1
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://t.me/$debug-invoice"
    callback.answer.assert_awaited_once_with("Debug invoice ready")


@pytest.mark.asyncio
async def test_debug_chat_callback_binds_only_current_shared_chat(
    make_message,
    make_user,
    mock_user_model,
    mock_chat_model,
):
    callback = _callback(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
    chat = mock_chat_model(
        chat_id=UUID(int=2),
        telegram_id=callback.message.chat.id,
        chat_type="supergroup",
    )
    handle = _debug_handle(PurchaseTarget.chat(chat.id))
    service = _purchase_intents(handle)

    await handle_debug_buy_callback(
        callback,
        DebugPurchaseCallback(
            product_id=DEFAULT_PRODUCT_CATALOG.debug_top_up.id,
            target=PurchaseTargetCode.CHAT,
        ),
        service,
        user,
        chat,
    )

    service.create_admin_debug_top_up_intent.assert_awaited_once_with(
        payer_user_id=user.id,
        target=PurchaseTarget.chat(chat.id),
    )
    assert "this chat" in callback.message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_debug_chat_callback_rejects_private_or_changed_chat(
    make_message,
    make_user,
    mock_user_model,
    mock_chat_model,
):
    callback = _callback(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
    chat = mock_chat_model(
        chat_id=UUID(int=2),
        telegram_id=callback.message.chat.id + 1,
        chat_type="supergroup",
    )
    service = _purchase_intents()

    await handle_debug_buy_callback(
        callback,
        DebugPurchaseCallback(
            product_id=DEFAULT_PRODUCT_CATALOG.debug_top_up.id,
            target=PurchaseTargetCode.CHAT,
        ),
        service,
        user,
        chat,
    )

    service.create_admin_debug_top_up_intent.assert_not_awaited()
    callback.bot.create_invoice_link.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "The shared chat target is unavailable",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_stale_legacy_debug_callback_fails_closed():
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "dbuy:test_small:user"
    callback.answer = AsyncMock()

    await reject_legacy_debug_buy_callback(callback)

    callback.answer.assert_awaited_once_with(
        "This debug purchase option expired. Run /debug_buy again.",
        show_alert=True,
    )
