"""Tests for payment handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from derp.credits.packs import CREDIT_PACKS
from derp.handlers.payments import (
    handle_buy_callback,
    handle_pre_checkout,
    handle_successful_payment,
)


class TestBuyCallback:
    """Tests for buy button callback handler."""

    @pytest.mark.asyncio
    async def test_creates_invoice(self, mock_db_client):
        """Test callback creates invoice link."""
        callback = MagicMock()
        callback.data = "buy:starter:user"
        callback.from_user.id = 12345
        callback.message = MagicMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()

        bot = MagicMock()
        bot.create_invoice_link = AsyncMock(return_value="https://t.me/invoice/xxx")

        await handle_buy_callback(callback, bot, None, None)

        bot.create_invoice_link.assert_awaited_once()
        callback.message.answer.assert_awaited_once()
        callback.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_callback_data(self):
        """Test callback with invalid data shows error."""
        callback = MagicMock()
        callback.data = "buy:invalid"
        callback.answer = AsyncMock()

        bot = MagicMock()

        await handle_buy_callback(callback, bot, None, None)

        callback.answer.assert_awaited_with("Invalid purchase request", show_alert=True)

    @pytest.mark.asyncio
    async def test_unknown_pack(self):
        """Test callback with unknown pack shows error."""
        callback = MagicMock()
        callback.data = "buy:nonexistent:user"
        callback.message = MagicMock()
        callback.answer = AsyncMock()

        bot = MagicMock()

        await handle_buy_callback(callback, bot, None, None)

        callback.answer.assert_awaited_with("Unknown credit pack", show_alert=True)


class TestPreCheckout:
    """Tests for pre-checkout handler."""

    @pytest.mark.asyncio
    async def test_approves_valid_checkout(self):
        """Test valid checkout is approved."""
        # Get a real pack ID
        pack_id = next(iter(CREDIT_PACKS.keys()))

        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = f"{pack_id}:user:12345"
        pre_checkout.from_user.id = 12345
        pre_checkout.total_amount = CREDIT_PACKS[pack_id].stars
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(pre_checkout)

        pre_checkout.answer.assert_awaited_with(ok=True)

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
    async def test_adds_user_credits(self, make_message, mock_db_client):
        """Test successful payment adds credits to user."""
        pack_id = next(iter(CREDIT_PACKS.keys()))
        pack = CREDIT_PACKS[pack_id]

        message = make_message(text="")
        message.successful_payment = MagicMock()
        message.successful_payment.invoice_payload = f"{pack_id}:user:12345"
        message.successful_payment.telegram_payment_charge_id = "charge_123"
        message.from_user = MagicMock()
        message.from_user.id = 12345

        user = MagicMock()
        user.id = "user-uuid"
        user.telegram_id = 12345

        with (
            patch("derp.handlers.payments.get_db_manager", return_value=mock_db_client),
            patch("derp.handlers.payments.CreditService") as mock_credit_service,
        ):
            service = mock_credit_service.return_value
            service.purchase_credits = AsyncMock(return_value=pack.credits)

            await handle_successful_payment(message, user, None)

            service.purchase_credits.assert_awaited_once()
            call_args = service.purchase_credits.call_args
            assert call_args[1]["user_id"] == user.id
            assert call_args[1]["amount"] == pack.credits

            message.answer.assert_awaited_once()
            response = message.answer.call_args[0][0]
            assert "Payment successful" in response

    @pytest.mark.asyncio
    async def test_adds_chat_credits(self, make_message, mock_db_client):
        """Test successful payment adds credits to chat."""
        pack_id = next(iter(CREDIT_PACKS.keys()))
        pack = CREDIT_PACKS[pack_id]

        message = make_message(text="")
        message.successful_payment = MagicMock()
        message.successful_payment.invoice_payload = f"{pack_id}:chat:-100123"
        message.successful_payment.telegram_payment_charge_id = "charge_123"
        message.from_user = MagicMock()
        message.from_user.id = 12345

        user = MagicMock()
        user.id = "user-uuid"
        user.telegram_id = 12345

        chat = MagicMock()
        chat.id = "chat-uuid"
        chat.telegram_id = -100123

        with (
            patch("derp.handlers.payments.get_db_manager", return_value=mock_db_client),
            patch("derp.handlers.payments.CreditService") as mock_credit_service,
        ):
            service = mock_credit_service.return_value
            service.purchase_credits = AsyncMock(return_value=pack.credits)

            await handle_successful_payment(message, user, chat)

            service.purchase_credits.assert_awaited_once()
            call_args = service.purchase_credits.call_args
            assert call_args[1]["chat_id"] == chat.id

    @pytest.mark.asyncio
    async def test_no_payment_object(self, make_message):
        """Test handler returns early without payment object."""
        message = make_message(text="")
        message.successful_payment = None

        await handle_successful_payment(message, None, None)

        message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_payload(self, make_message):
        """Test invalid payload shows error."""
        message = make_message(text="")
        message.successful_payment = MagicMock()
        message.successful_payment.invoice_payload = "invalid"
        message.from_user = MagicMock()
        message.from_user.id = 12345

        await handle_successful_payment(message, None, None)

        message.answer.assert_awaited_once()
        assert "could not be added" in message.answer.call_args[0][0]
