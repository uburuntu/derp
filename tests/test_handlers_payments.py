"""Tests for payment handler."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from derp.credits.packs import CREDIT_PACKS
from derp.handlers.payments import (
    handle_buy_callback,
    handle_pre_checkout,
    handle_successful_payment,
)


def _get_text_from_call_args(call_args):
    """Extract text from mock call_args (handles both positional and keyword)."""
    if call_args.args:
        return call_args.args[0]
    if call_args.kwargs and "text" in call_args.kwargs:
        return call_args.kwargs["text"]
    return ""


class TestBuyCallback:
    """Tests for buy button callback handler."""

    @pytest.mark.asyncio
    async def test_rejects_stale_valid_button_without_sending_invoice(self):
        callback = MagicMock()
        callback.data = "buy:starter:user"
        callback.from_user.id = 12345
        callback.message = MagicMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()

        await handle_buy_callback(callback)

        callback.message.answer.assert_not_awaited()
        callback.answer.assert_awaited_once()
        assert "temporarily unavailable" in callback.answer.await_args.args[0]
        assert callback.answer.await_args.kwargs == {"show_alert": True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", ["buy:invalid", "buy:nonexistent:user"])
    async def test_all_legacy_buttons_fail_closed(self, data):
        callback = MagicMock()
        callback.data = data
        callback.from_user.id = 12345
        callback.answer = AsyncMock()

        await handle_buy_callback(callback)

        assert callback.answer.await_args.kwargs == {"show_alert": True}


class TestPreCheckout:
    """Tests for pre-checkout handler."""

    @pytest.mark.asyncio
    async def test_rejects_legacy_valid_checkout(self):
        # Get a real pack ID
        pack_id = next(iter(CREDIT_PACKS.keys()))

        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = f"{pack_id}:user:12345"
        pre_checkout.from_user.id = 12345
        pre_checkout.total_amount = CREDIT_PACKS[pack_id].stars
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(pre_checkout)

        pre_checkout.answer.assert_awaited_once()
        assert pre_checkout.answer.await_args.kwargs["ok"] is False
        assert (
            "temporarily unavailable"
            in pre_checkout.answer.await_args.kwargs["error_message"]
        )

    @pytest.mark.asyncio
    async def test_rejects_invalid_payload(self):
        """Test invalid payload is rejected."""
        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = "invalid"
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(pre_checkout)

        pre_checkout.answer.assert_awaited()
        call_args = pre_checkout.answer.call_args
        assert call_args[1]["ok"] is False

    @pytest.mark.asyncio
    async def test_rejects_unknown_pack(self):
        """Test unknown pack is rejected."""
        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = "unknown_pack:user:12345"
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(pre_checkout)

        pre_checkout.answer.assert_awaited()
        call_args = pre_checkout.answer.call_args
        assert call_args[1]["ok"] is False


class TestSuccessfulPayment:
    """Tests for successful payment handler."""

    @pytest.mark.asyncio
    async def test_adds_user_credits(
        self, make_message, mock_sender, mock_user_model, mock_credit_service_factory
    ):
        """Test successful payment adds credits to user."""
        pack_id = next(iter(CREDIT_PACKS.keys()))
        pack = CREDIT_PACKS[pack_id]

        message = make_message(text="")
        sender = mock_sender(message=message)
        message.successful_payment = MagicMock()
        message.successful_payment.invoice_payload = f"{pack_id}:user:12345"
        message.successful_payment.telegram_payment_charge_id = "charge_123"
        message.from_user = MagicMock()
        message.from_user.id = 12345

        user = mock_user_model(telegram_id=12345)
        service = mock_credit_service_factory(purchase_result=pack.credits)

        await handle_successful_payment(
            message, sender, service, user_model=user, chat_model=None
        )

        service.purchase_credits.assert_awaited_once()
        service.purchase_credits.assert_awaited_once_with(
            user,
            None,
            pack.credits,
            "charge_123",
            pack_name=pack.name,
        )
        sender.send.assert_awaited_once()
        text = _get_text_from_call_args(sender.send.call_args)
        assert "Payment successful" in text

    @pytest.mark.asyncio
    async def test_adds_chat_credits(
        self,
        make_message,
        mock_sender,
        mock_user_model,
        mock_chat_model,
        mock_credit_service_factory,
    ):
        """Test successful payment adds credits to chat."""
        pack_id = next(iter(CREDIT_PACKS.keys()))
        pack = CREDIT_PACKS[pack_id]

        message = make_message(text="")
        sender = mock_sender(message=message)
        message.successful_payment = MagicMock()
        message.successful_payment.invoice_payload = f"{pack_id}:chat:-100123"
        message.successful_payment.telegram_payment_charge_id = "charge_123"
        message.from_user = MagicMock()
        message.from_user.id = 12345

        user = mock_user_model(telegram_id=12345)
        chat = mock_chat_model(telegram_id=-100123)
        service = mock_credit_service_factory(purchase_result=pack.credits)

        await handle_successful_payment(
            message, sender, service, user_model=user, chat_model=chat
        )

        service.purchase_credits.assert_awaited_once_with(
            user,
            chat,
            pack.credits,
            "charge_123",
            pack_name=pack.name,
        )

    @pytest.mark.asyncio
    async def test_no_payment_object(
        self, make_message, mock_sender, mock_credit_service_factory
    ):
        """Test handler returns early without payment object."""
        message = make_message(text="")
        sender = mock_sender(message=message)
        message.successful_payment = None
        service = mock_credit_service_factory()

        await handle_successful_payment(
            message, sender, service, user_model=None, chat_model=None
        )

        message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_payload(
        self, make_message, mock_sender, mock_credit_service_factory
    ):
        """Test invalid payload shows error."""
        message = make_message(text="")
        sender = mock_sender(message=message)
        message.successful_payment = MagicMock()
        message.successful_payment.invoice_payload = "invalid"
        message.from_user = MagicMock()
        message.from_user.id = 12345
        service = mock_credit_service_factory()

        await handle_successful_payment(
            message, sender, service, user_model=None, chat_model=None
        )

        message.answer.assert_awaited_once()
        text = _get_text_from_call_args(message.answer.call_args)
        assert "could not be added" in text
