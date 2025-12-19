"""Tests for debug handler commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.handlers.debug import (
    DEBUG_PACKS,
    DebugPayload,
    _build_debug_buy_keyboard,
    debug_add_credits,
    debug_buy_command,
    debug_help,
    debug_refund,
    debug_status,
    debug_tools,
    handle_debug_buy_callback,
    handle_debug_pre_checkout,
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


class TestBuildDebugBuyKeyboard:
    """Tests for keyboard builder."""

    def test_builds_keyboard_without_chat(self):
        """Test keyboard without chat context."""
        keyboard = _build_debug_buy_keyboard(chat_id=None)
        assert keyboard.inline_keyboard
        # Should have 3 buttons (one per pack for user only)
        assert len(keyboard.inline_keyboard) == 3

    def test_builds_keyboard_with_chat(self):
        """Test keyboard with chat context includes chat buttons."""
        keyboard = _build_debug_buy_keyboard(chat_id=-100123)
        # Should have 6 buttons (3 user + 3 chat)
        assert len(keyboard.inline_keyboard) == 6


@pytest.mark.asyncio
async def test_debug_buy_command(make_message):
    """Test /debug_buy command shows menu."""
    message = make_message(text="/debug_buy")

    chat_model = MagicMock()
    chat_model.telegram_id = -100123

    await debug_buy_command(message, chat_model)

    message.reply.assert_awaited_once()
    call_args = message.reply.call_args
    assert "Debug Buy Menu" in call_args[0][0]
    assert call_args[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_debug_add_credits(
    make_message, mock_user_model, mock_credit_service_factory
):
    """Test /debug_credits command adds credits."""
    message = make_message(text="/debug_credits 50")

    user = mock_user_model(telegram_id=12345)
    service = mock_credit_service_factory(purchase_result=50)

    await debug_add_credits(message, service, user, None)

    service.purchase_credits.assert_awaited_once()
    message.reply.assert_awaited_once()
    assert "Added **50** credits" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_debug_add_credits_no_user(make_message, mock_credit_service_factory):
    """Test /debug_credits without user returns error."""
    message = make_message(text="/debug_credits 50")
    service = mock_credit_service_factory()

    await debug_add_credits(message, service, None, None)

    message.reply.assert_awaited_once()
    assert "User not found" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_debug_status(
    make_message, mock_user_model, mock_chat_model, mock_credit_service_factory
):
    """Test /debug_status shows diagnostics."""
    message = make_message(text="/debug_status")

    user = mock_user_model(telegram_id=12345)
    chat = mock_chat_model(telegram_id=-100123)
    chat.llm_memory = "Test memory"

    service = mock_credit_service_factory()

    with patch(
        "derp.handlers.debug.get_balances", new_callable=AsyncMock
    ) as mock_balances:
        mock_balances.return_value = (100, 50)  # chat_credits, user_credits

        await debug_status(message, service, user, chat)

        message.reply.assert_awaited_once()
        response = message.reply.call_args[0][0]
        assert "Debug Status" in response
        assert "12345" in response  # user telegram id


@pytest.mark.asyncio
async def test_debug_status_no_user(make_message, mock_credit_service_factory):
    """Test /debug_status without user returns error."""
    message = make_message(text="/debug_status")
    service = mock_credit_service_factory()

    await debug_status(message, service, None, None)

    message.reply.assert_awaited_once()
    assert "User not found" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_debug_refund(make_message, mock_credit_service_factory):
    """Test /debug_refund processes refund."""
    message = make_message(text="/debug_refund charge_123")
    service = mock_credit_service_factory()
    service.refund_credits = AsyncMock(return_value=True)

    await debug_refund(message, service)

    service.refund_credits.assert_awaited_once_with("charge_123")
    message.reply.assert_awaited_once()
    assert "Refund processed" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_debug_tools(make_message):
    """Test /debug_tools lists available tools."""
    message = make_message(text="/debug_tools")

    await debug_tools(message)

    message.reply.assert_awaited_once()
    response = message.reply.call_args[0][0]
    assert "Available Tools" in response


@pytest.mark.asyncio
async def test_debug_help(make_message):
    """Test /debug_help shows all commands."""
    message = make_message(text="/debug_help")

    await debug_help(message)

    message.reply.assert_awaited_once()
    response = message.reply.call_args[0][0]
    assert "Debug Commands" in response
    assert "/debug_buy" in response
    assert "/debug_credits" in response


@pytest.mark.asyncio
async def test_handle_debug_buy_callback():
    """Test debug buy callback creates invoice."""
    callback = MagicMock()
    callback.data = "dbuy:test_small:user"
    callback.from_user.id = 12345
    callback.message = MagicMock()
    callback.message.answer_invoice = AsyncMock()
    callback.answer = AsyncMock()

    bot = MagicMock()

    await handle_debug_buy_callback(callback, bot)

    callback.message.answer_invoice.assert_awaited_once()
    call_args = callback.message.answer_invoice.call_args
    assert call_args[1]["currency"] == "XTR"
    assert call_args[1]["prices"][0].amount == 1  # 1 star


@pytest.mark.asyncio
async def test_handle_debug_buy_callback_invalid_pack():
    """Test debug buy callback with invalid pack."""
    callback = MagicMock()
    callback.data = "dbuy:invalid_pack:user"
    callback.from_user.id = 12345
    callback.message = MagicMock()
    callback.answer = AsyncMock()

    bot = MagicMock()

    await handle_debug_buy_callback(callback, bot)

    callback.answer.assert_awaited_with("Unknown debug pack", show_alert=True)


@pytest.mark.asyncio
async def test_handle_debug_pre_checkout():
    """Test pre-checkout approval."""
    pre_checkout = MagicMock()
    pre_checkout.invoice_payload = (
        '{"k":"debug_credits","p":"test_small","tt":"user","ti":12345}'
    )
    pre_checkout.total_amount = 1
    pre_checkout.from_user.id = 12345
    pre_checkout.answer = AsyncMock()

    await handle_debug_pre_checkout(pre_checkout)

    pre_checkout.answer.assert_awaited_with(ok=True)
