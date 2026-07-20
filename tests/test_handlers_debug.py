"""Tests for debug handler commands."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from derp.handlers.debug import (
    DEBUG_PACKS,
    DebugPayload,
    debug_add_credits,
    debug_buy_command,
    debug_help,
    debug_refund,
    debug_status,
    debug_tools,
    handle_debug_buy_callback,
    handle_debug_pre_checkout,
    handle_debug_successful_payment,
)


class TestDebugPayload:
    """Tests for DebugPayload model."""

    def test_payload_creation(self):
        """Test creating a debug payload."""
        payload = DebugPayload(
            pack_id="test_small", target_type="user", target_id=12345
        )
        assert payload.kind == "debug_credits"
        assert payload.pack_id == "test_small"
        assert payload.target_type == "user"
        assert payload.target_id == 12345

    def test_payload_serialization(self):
        """Test payload serializes with aliases."""
        payload = DebugPayload(
            pack_id="test_small", target_type="user", target_id=12345
        )
        json_str = payload.model_dump_json(by_alias=True)
        assert '"k":"debug_credits"' in json_str
        assert '"p":"test_small"' in json_str


class TestDebugPacks:
    """Tests for debug credit packs."""

    def test_packs_exist(self):
        """Test that debug packs are defined."""
        assert "test_small" in DEBUG_PACKS
        assert "test_medium" in DEBUG_PACKS
        assert "test_large" in DEBUG_PACKS

    def test_packs_cost_one_star(self):
        """All debug packs should cost 1 star."""
        for pack in DEBUG_PACKS.values():
            assert pack.stars == 1


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


@pytest.mark.asyncio
async def test_debug_buy_command(make_message, mock_sender):
    """Test /debug_buy is suspended without exposing a keyboard."""
    message = make_message(text="/debug_buy")
    sender = mock_sender(message=message)

    await debug_buy_command(message, sender)

    sender.reply.assert_awaited_once()
    call_args = sender.reply.call_args
    text = _get_text_from_call_args(call_args)
    assert "temporarily unavailable" in text
    assert call_args.kwargs.get("reply_markup") is None


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
    assert "/debug_buy" not in response
    assert "/debug_credits" in response


@pytest.mark.asyncio
async def test_handle_debug_buy_callback():
    """Test stale debug buy callback cannot create an invoice."""
    callback = MagicMock()
    callback.data = "dbuy:test_small:user"
    callback.from_user.id = 12345
    callback.message = MagicMock()
    callback.message.answer_invoice = AsyncMock()
    callback.answer = AsyncMock()

    await handle_debug_buy_callback(callback)

    callback.message.answer_invoice.assert_not_awaited()
    assert "temporarily unavailable" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs == {"show_alert": True}


@pytest.mark.asyncio
async def test_handle_debug_buy_callback_invalid_pack():
    """Test malformed legacy debug callbacks fail closed identically."""
    callback = MagicMock()
    callback.data = "dbuy:invalid_pack:user"
    callback.from_user.id = 12345
    callback.message = MagicMock()
    callback.answer = AsyncMock()

    await handle_debug_buy_callback(callback)

    assert "temporarily unavailable" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs == {"show_alert": True}


@pytest.mark.asyncio
async def test_handle_debug_pre_checkout():
    """Test issued debug invoices are rejected before capture."""
    pre_checkout = MagicMock()
    pre_checkout.invoice_payload = (
        '{"k":"debug_credits","p":"test_small","tt":"user","ti":12345}'
    )
    pre_checkout.total_amount = 1
    pre_checkout.from_user.id = 12345
    pre_checkout.answer = AsyncMock()

    await handle_debug_pre_checkout(pre_checkout)

    assert pre_checkout.answer.await_args.kwargs["ok"] is False
    assert (
        "temporarily unavailable"
        in pre_checkout.answer.await_args.kwargs["error_message"]
    )


@pytest.mark.asyncio
async def test_handle_debug_successful_payment_still_reconciles(
    make_message, mock_sender, mock_user_model, mock_credit_service_factory
):
    message = make_message(text="")
    sender = mock_sender(message=message)
    user = mock_user_model(telegram_id=12345)
    service = mock_credit_service_factory(purchase_result=10)
    payload = DebugPayload(
        pack_id="test_small",
        target_type="user",
        target_id=12345,
    )
    message.successful_payment = MagicMock(
        invoice_payload=payload.model_dump_json(by_alias=True),
        telegram_payment_charge_id="debug-charge-123",
    )

    await handle_debug_successful_payment(
        message,
        sender,
        service,
        user_model=user,
        chat_model=None,
    )

    service.purchase_credits.assert_awaited_once_with(
        user,
        None,
        10,
        "debug-charge-123",
        pack_name="DEBUG:Test Small",
    )
    sender.send.assert_awaited_once()
