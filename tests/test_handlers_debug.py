"""Tests for operator-only durable one-Star purchase validation."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiogram.types import CallbackQuery

from derp.billing import PurchaseIntentHandle, PurchaseTarget
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseTargetCode
from derp.handlers.debug import (
    RETIRED_DEBUG_COMMANDS,
    DebugPurchaseCallback,
    debug_buy_command,
    handle_debug_buy_callback,
    reject_legacy_debug_buy_callback,
    reject_unauthorized_debug_callback,
    reject_unauthorized_debug_command,
    retired_debug_command,
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
    service.create_operator_debug_top_up_intent = AsyncMock(return_value=handle)
    return service


def _callback(make_message, make_user) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = make_message(
        text="debug purchase",
        user_id=12345,
        chat_id=12345,
        chat_type="private",
    )
    callback.message.business_connection_id = "business-1"
    callback.from_user = make_user(id=12345)
    callback.bot = MagicMock()
    callback.bot.create_invoice_link = AsyncMock(
        return_value="https://t.me/$debug-invoice"
    )
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_debug_buy_command_shows_only_personal_target_in_private_chat(
    make_message,
    mock_sender,
):
    message = make_message(
        text="/debug_buy",
        user_id=12345,
        chat_id=12345,
        chat_type="private",
    )
    sender = mock_sender(message=message)

    await debug_buy_command(message, sender)

    assert sender.protect_content is True
    text = sender.reply.await_args.args[0]
    assert "1 Star" in text
    buttons = sender.reply.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    callback = DebugPurchaseCallback.unpack(buttons[0][0].callback_data)
    assert callback.product_id == DEFAULT_PRODUCT_CATALOG.debug_top_up.id
    assert callback.target is PurchaseTargetCode.USER


@pytest.mark.asyncio
async def test_debug_buy_command_redirects_group_use_to_private_chat(
    make_message,
    mock_sender,
):
    message = make_message(text="/debug_buy")
    sender = mock_sender(message=message)

    await debug_buy_command(message, sender)

    sender.reply.assert_awaited_once_with(
        "Operator controls are private. Open /operator in your private chat."
    )


@pytest.mark.asyncio
async def test_retired_debug_controls_redirect_without_legacy_dependencies(
    make_message,
    mock_sender,
):
    message = make_message(text="/debug_refund sensitive-charge-id")
    sender = mock_sender(message=message)

    await retired_debug_command(message, sender)

    sender.reply.assert_awaited_once_with("Use /operator for diagnostics and controls.")
    assert {
        "debug_credits",
        "debug_reset",
        "debug_refund",
        "debug_status",
        "debug_tools",
        "debug_help",
    } <= set(RETIRED_DEBUG_COMMANDS)


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

    service.create_operator_debug_top_up_intent.assert_awaited_once_with(
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
    assert callback.message.answer.await_args.kwargs["protect_content"] is True
    callback.answer.assert_awaited_once_with("Invoice ready")


@pytest.mark.asyncio
async def test_legacy_debug_chat_callback_is_no_longer_available(
    make_message,
    make_user,
    mock_user_model,
):
    callback = _callback(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
    service = _purchase_intents()

    await handle_debug_buy_callback(
        callback,
        DebugPurchaseCallback(
            product_id=DEFAULT_PRODUCT_CATALOG.debug_top_up.id,
            target=PurchaseTargetCode.CHAT,
        ),
        service,
        user,
    )

    service.create_operator_debug_top_up_intent.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "This chat is unavailable",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_debug_callback_rejects_group_context_before_creating_intent(
    make_message,
    make_user,
    mock_user_model,
):
    callback = _callback(make_message, make_user)
    callback.message.chat.type = "supergroup"
    callback.message.chat.id = -10042
    user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
    service = _purchase_intents()

    await handle_debug_buy_callback(
        callback,
        DebugPurchaseCallback(
            product_id=DEFAULT_PRODUCT_CATALOG.debug_top_up.id,
            target=PurchaseTargetCode.USER,
        ),
        service,
        user,
    )

    service.create_operator_debug_top_up_intent.assert_not_awaited()
    callback.bot.create_invoice_link.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Operator controls are private. Open /operator in your private chat.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_stale_legacy_debug_callback_fails_closed():
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "dbuy:test_small:user"
    callback.answer = AsyncMock()

    await reject_legacy_debug_buy_callback(callback)

    callback.answer.assert_awaited_once_with(
        "This option expired. Run /debug_buy again.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_unauthorized_debug_input_is_consumed_and_callback_answered(
    make_message,
):
    assert (
        await reject_unauthorized_debug_command(make_message(text="/debug_status"))
        is None
    )
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()

    await reject_unauthorized_debug_callback(callback)

    callback.answer.assert_awaited_once_with(
        "This action is unavailable",
        show_alert=True,
    )
