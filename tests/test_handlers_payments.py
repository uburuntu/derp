"""Tests for the durable Telegram Stars payment adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from derp.billing import (
    ActiveSubscriptionError,
    CapturedPayment,
    ClawbackResult,
    CommercePolicy,
    FulfillmentResult,
    FulfillmentState,
    PaymentConflictError,
    PaymentReplyDisposition,
    PaymentSettlementState,
    PaymentUpdateDisposition,
    PaymentUpdateKind,
    PaymentUpdateOutcome,
    PreCheckoutDecision,
    PreCheckoutRejection,
    PreCheckoutRequest,
    ProductKind,
    PurchaseIntentHandle,
    PurchaseTarget,
)
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseCallback, PurchaseTargetCode
from derp.billing.types import RefundedPaymentCommand
from derp.handlers.payments import (
    handle_buy_callback,
    handle_pre_checkout,
    handle_refunded_payment,
    handle_successful_payment,
    reject_malformed_buy_callback,
)
from derp.legal import TermsAcceptanceRequiredError
from derp.observability import telemetry_fingerprint
from derp.operator import OperatorAccessPolicy

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)
OPEN_COMMERCE = CommercePolicy(public_intake_enabled=True)


def _intent(
    *,
    kind: ProductKind = ProductKind.TOP_UP,
    target: PurchaseTarget | None = None,
    product_id: str = "starter",
    stars: int | None = None,
) -> PurchaseIntentHandle:
    product = (
        DEFAULT_PRODUCT_CATALOG.subscription_plan
        if kind is ProductKind.SUBSCRIPTION
        else DEFAULT_PRODUCT_CATALOG.current_top_ups[product_id]
    )
    return PurchaseIntentHandle(
        intent_id=UUID(int=10),
        invoice_payload="dpi1_opaque-token",
        product_kind=kind,
        product_id=product.id,
        product_version=product.version,
        target=target or PurchaseTarget.user(UUID(int=1)),
        credits=product.credits,
        stars=product.stars if stars is None else stars,
        currency=product.currency,
        expires_at=NOW,
        subscription_period_seconds=getattr(product, "period_seconds", None),
    )


def _purchase_intents(*, handle: PurchaseIntentHandle | None = None) -> MagicMock:
    service = MagicMock()
    service.create_top_up_intent = AsyncMock(return_value=handle)
    service.create_subscription_intent = AsyncMock(return_value=handle)
    service.validate_pre_checkout = AsyncMock()
    return service


def _callback(make_message, make_user) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = make_message(text="purchase")
    callback.message.business_connection_id = "business-1"
    callback.from_user = make_user(id=12345)
    callback.bot = MagicMock()
    callback.bot.create_invoice_link = AsyncMock(
        return_value="https://t.me/$invoice-link"
    )
    callback.answer = AsyncMock()
    return callback


class TestBuyCallback:
    @pytest.mark.asyncio
    async def test_public_intake_is_fail_closed_by_default(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        service = _purchase_intents()
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            mock_user_model(telegram_id=12345),
        )

        service.create_top_up_intent.assert_not_awaited()
        callback.bot.create_invoice_link.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "Credit purchases aren't available yet. You won't be charged.",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_missing_user_fails_without_charge(
        self,
        make_message,
        make_user,
    ) -> None:
        service = _purchase_intents()
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            commerce_policy=OPEN_COMMERCE,
        )

        service.create_top_up_intent.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "This purchase is unavailable. You won't be charged. "
            "Open /buy and try again.",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_current_terms_are_required_before_invoice_creation(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        service = _purchase_intents()
        service.create_top_up_intent.side_effect = TermsAcceptanceRequiredError
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            mock_user_model(user_id=UUID(int=1), telegram_id=12345),
            commerce_policy=OPEN_COMMERCE,
        )

        callback.bot.create_invoice_link.assert_not_awaited()
        assert "Terms and privacy" in callback.message.answer.await_args.args[0]
        callback.answer.assert_awaited_once_with(
            "Review and accept the current terms before buying.",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_personal_top_up_creates_bound_intent_and_invoice_link(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        handle = _intent(target=PurchaseTarget.user(user.id))
        service = _purchase_intents(handle=handle)
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            user,
            commerce_policy=OPEN_COMMERCE,
        )

        service.create_top_up_intent.assert_awaited_once_with(
            payer_user_id=user.id,
            target=PurchaseTarget.user(user.id),
            product_id="starter",
        )
        callback.bot.create_invoice_link.assert_awaited_once()
        invoice = callback.bot.create_invoice_link.await_args.kwargs
        assert invoice["payload"] == handle.invoice_payload
        assert invoice["currency"] == "XTR"
        assert invoice["prices"][0].amount == handle.stars
        assert invoice["provider_token"] == ""
        assert invoice["business_connection_id"] == "business-1"
        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].url == "https://t.me/$invoice-link"
        callback.answer.assert_awaited_once_with("Invoice ready")

    @pytest.mark.asyncio
    async def test_chat_top_up_binds_current_non_private_chat(
        self,
        make_message,
        make_user,
        mock_user_model,
        mock_chat_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        chat = mock_chat_model(chat_id=UUID(int=2), chat_type="supergroup")
        handle = _intent(target=PurchaseTarget.chat(chat.id))
        service = _purchase_intents(handle=handle)
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.CHAT,
            ),
            service,
            user,
            chat,
            commerce_policy=OPEN_COMMERCE,
        )

        service.create_top_up_intent.assert_awaited_once_with(
            payer_user_id=user.id,
            target=PurchaseTarget.chat(chat.id),
            product_id="starter",
        )
        assert "this chat" in callback.message.answer.await_args.args[0]

    @pytest.mark.parametrize(
        ("stars", "expected"),
        [(1, "1 Star"), (2, "2 Stars")],
    )
    @pytest.mark.asyncio
    async def test_invoice_pluralizes_stars(
        self,
        make_message,
        make_user,
        mock_user_model,
        stars: int,
        expected: str,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        service = _purchase_intents(
            handle=_intent(target=PurchaseTarget.user(user.id), stars=stars)
        )
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            user,
            commerce_policy=OPEN_COMMERCE,
        )

        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == f"Pay {expected}"
        assert expected in callback.message.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_subscription_uses_personal_plan_intent(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        handle = _intent(
            kind=ProductKind.SUBSCRIPTION,
            target=PurchaseTarget.user(user.id),
            product_id=DEFAULT_PRODUCT_CATALOG.subscription_plan.id,
        )
        service = _purchase_intents(handle=handle)
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.SUBSCRIPTION,
                product_id=handle.product_id,
                target=PurchaseTargetCode.USER,
            ),
            service,
            user,
            commerce_policy=OPEN_COMMERCE,
        )

        service.create_subscription_intent.assert_awaited_once_with(
            payer_user_id=user.id
        )
        service.create_top_up_intent.assert_not_awaited()
        invoice = callback.bot.create_invoice_link.await_args.kwargs
        assert invoice["subscription_period"] == 30 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_changed_actor_fails_before_creating_intent(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=999)
        service = _purchase_intents()
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            user,
            commerce_policy=OPEN_COMMERCE,
        )

        service.create_top_up_intent.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "This purchase is no longer valid. You won't be charged. Open /buy again.",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_active_subscription_returns_specific_alert(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        service = _purchase_intents()
        service.create_subscription_intent.side_effect = ActiveSubscriptionError
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.SUBSCRIPTION,
                product_id=DEFAULT_PRODUCT_CATALOG.subscription_plan.id,
                target=PurchaseTargetCode.USER,
            ),
            service,
            user,
            commerce_policy=OPEN_COMMERCE,
        )

        callback.answer.assert_awaited_once_with(
            "Your monthly plan is already active", show_alert=True
        )
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_option_fails_without_charge(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        service = _purchase_intents()
        service.create_top_up_intent.side_effect = LookupError("gone")
        callback = _callback(make_message, make_user)

        await handle_buy_callback(
            callback,
            PurchaseCallback(
                kind=ProductKind.TOP_UP,
                product_id="starter",
                target=PurchaseTargetCode.USER,
            ),
            service,
            user,
            commerce_policy=OPEN_COMMERCE,
        )

        callback.answer.assert_awaited_once_with(
            "This purchase option is no longer available. You won't be charged. "
            "Open /buy again.",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_invoice_failure_is_explicitly_not_charged(
        self,
        make_message,
        make_user,
        mock_user_model,
    ) -> None:
        user = mock_user_model(user_id=UUID(int=1), telegram_id=12345)
        service = _purchase_intents(handle=_intent(target=PurchaseTarget.user(user.id)))
        callback = _callback(make_message, make_user)

        with patch(
            "derp.handlers.payments.create_stars_invoice_link",
            new=AsyncMock(
                side_effect=TelegramAPIError(
                    method=MagicMock(),
                    message="invoice unavailable",
                )
            ),
        ):
            await handle_buy_callback(
                callback,
                PurchaseCallback(
                    kind=ProductKind.TOP_UP,
                    product_id="starter",
                    target=PurchaseTargetCode.USER,
                ),
                service,
                user,
                commerce_policy=OPEN_COMMERCE,
            )

        callback.answer.assert_awaited_once_with(
            "Telegram couldn't prepare the invoice. You won't be charged. Try again.",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_stale_callback_fails_closed(self) -> None:
        callback = MagicMock(spec=CallbackQuery)
        callback.answer = AsyncMock()

        await reject_malformed_buy_callback(callback)

        callback.answer.assert_awaited_once_with(
            "This purchase option expired. You weren't charged. Open /buy again.",
            show_alert=True,
        )


class TestPreCheckout:
    @pytest.mark.asyncio
    async def test_approved_decision_maps_every_telegram_field(self, make_user) -> None:
        intent_id = UUID(int=20)
        service = _purchase_intents()
        service.validate_pre_checkout.return_value = PreCheckoutDecision(
            approved=True,
            intent_id=intent_id,
        )
        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = "dpi1_opaque-token"
        pre_checkout.from_user = make_user(id=12345)
        pre_checkout.currency = "XTR"
        pre_checkout.total_amount = 150
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(
            pre_checkout,
            service,
            commerce_policy=OPEN_COMMERCE,
        )

        service.validate_pre_checkout.assert_awaited_once_with(
            PreCheckoutRequest(
                invoice_payload="dpi1_opaque-token",
                payer_telegram_id=12345,
                currency="XTR",
                total_amount=150,
            ),
            public_intake_enabled=True,
            operator_debug_allowed=False,
        )
        pre_checkout.answer.assert_awaited_once_with(ok=True)

    @pytest.mark.asyncio
    async def test_closed_intake_only_exposes_operator_debug_exception(
        self, make_user
    ) -> None:
        service = _purchase_intents()
        service.validate_pre_checkout.return_value = PreCheckoutDecision(
            approved=True,
            intent_id=UUID(int=21),
        )
        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = "dpi1_debug-token"
        pre_checkout.from_user = make_user(id=12345)
        pre_checkout.currency = "XTR"
        pre_checkout.total_amount = 1
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(
            pre_checkout,
            service,
            operator_access=OperatorAccessPolicy.from_ids((12345,)),
        )

        assert service.validate_pre_checkout.await_args.kwargs == {
            "public_intake_enabled": False,
            "operator_debug_allowed": True,
        }

    @pytest.mark.asyncio
    async def test_rejected_decision_fails_checkout_closed(self, make_user) -> None:
        service = _purchase_intents()
        service.validate_pre_checkout.return_value = PreCheckoutDecision(
            approved=False,
            rejection=PreCheckoutRejection.EXPIRED,
        )
        pre_checkout = MagicMock()
        pre_checkout.invoice_payload = "dpi1_expired-token"
        pre_checkout.from_user = make_user(id=12345)
        pre_checkout.currency = "XTR"
        pre_checkout.total_amount = 50
        pre_checkout.answer = AsyncMock()

        await handle_pre_checkout(pre_checkout, service)

        pre_checkout.answer.assert_awaited_once_with(
            ok=False,
            error_message=(
                "This invoice expired or changed. You won't be charged. "
                "Open /buy and try again."
            ),
        )


def _payment_message(make_message, *, chat_type="private", **overrides):
    message = make_message(
        text="",
        chat_type=chat_type,
        chat_id=12345 if chat_type == "private" else -1001234567890,
    )
    values = {
        "invoice_payload": "dpi1_opaque-token",
        "telegram_payment_charge_id": "telegram-charge-1",
        "provider_payment_charge_id": "provider-charge-1",
        "currency": "XTR",
        "total_amount": 500,
        "is_recurring": False,
        "is_first_recurring": False,
        "subscription_expiration_date": None,
    }
    values.update(overrides)
    message.successful_payment = MagicMock(**values)
    return message


def _settlement(result: FulfillmentResult | None = None) -> MagicMock:
    service = MagicMock()
    service.fulfill = AsyncMock(return_value=result)
    return service


def _refund_message(make_message, *, chat_type="private", **overrides):
    message = make_message(
        text="",
        chat_type=chat_type,
        chat_id=12345 if chat_type == "private" else -1001234567890,
    )
    values = {
        "invoice_payload": "dpi1_opaque-token",
        "telegram_payment_charge_id": "telegram-refund-1",
        "provider_payment_charge_id": None,
        "currency": "XTR",
        "total_amount": 150,
    }
    values.update(overrides)
    message.refunded_payment = MagicMock(**values)
    return message


def _refund_settlement(result: ClawbackResult | None = None) -> MagicMock:
    service = MagicMock()
    service.clawback = AsyncMock(return_value=result)
    return service


class TestSuccessfulPayment:
    @pytest.mark.asyncio
    async def test_maps_captured_subscription_and_reports_active_allowance(
        self,
        make_message,
        mock_sender,
    ) -> None:
        expires_at = datetime(2026, 8, 19, 12, tzinfo=UTC)
        message = _payment_message(
            make_message,
            is_recurring=True,
            is_first_recurring=True,
            subscription_expiration_date=int(expires_at.timestamp()),
        )
        sender = mock_sender(message=message)
        result = FulfillmentResult(
            receipt_id=UUID(int=30),
            state=FulfillmentState.FULFILLED,
            subscription_cycle_id=UUID(int=31),
            available_credits=1_000,
        )
        settlement = _settlement(result)

        await handle_successful_payment(message, sender, settlement)

        settlement.fulfill.assert_awaited_once_with(
            CapturedPayment(
                invoice_payload="dpi1_opaque-token",
                telegram_charge_id="telegram-charge-1",
                provider_charge_id="provider-charge-1",
                payer_telegram_id=12345,
                currency="XTR",
                total_amount=500,
                is_recurring=True,
                is_first_recurring=True,
                subscription_expiration_at=expires_at,
            )
        )
        assert "Plan active" in sender.reply.await_args.args[0]
        assert "1000 monthly credits" in sender.reply.await_args.args[0]

    @pytest.mark.asyncio
    async def test_top_up_reports_available_value_and_debt_offset(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _payment_message(make_message, total_amount=150)
        sender = mock_sender(message=message)
        settlement = _settlement(
            FulfillmentResult(
                receipt_id=UUID(int=40),
                state=FulfillmentState.FULFILLED,
                wallet_lot_id=UUID(int=41),
                available_credits=135,
                debt_offset_credits=30,
            )
        )

        await handle_successful_payment(message, sender, settlement)

        text = sender.reply.await_args.args[0]
        assert "Payment complete" in text
        assert "Purchased credits available: 135" in text
        assert "Used to repay payment debt: 30 credits" in text

    @pytest.mark.parametrize(
        ("credits", "expected"),
        [(1, "Purchased credit available: 1"), (2, "Purchased credits available: 2")],
    )
    @pytest.mark.asyncio
    async def test_top_up_pluralizes_available_credits(
        self,
        make_message,
        mock_sender,
        credits: int,
        expected: str,
    ) -> None:
        message = _payment_message(make_message)
        sender = mock_sender(message=message)
        settlement = _settlement(
            FulfillmentResult(
                receipt_id=UUID(int=44),
                state=FulfillmentState.FULFILLED,
                wallet_lot_id=UUID(int=45),
                available_credits=credits,
            )
        )

        await handle_successful_payment(message, sender, settlement)

        assert expected in sender.reply.await_args.args[0]

    @pytest.mark.asyncio
    async def test_group_payment_sends_amounts_only_to_protected_private_chat(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _payment_message(make_message, chat_type="supergroup")
        sender = mock_sender(message=message)
        settlement = _settlement(
            FulfillmentResult(
                receipt_id=UUID(int=42),
                state=FulfillmentState.FULFILLED,
                wallet_lot_id=UUID(int=43),
                available_credits=135,
                debt_offset_credits=30,
            )
        )

        with patch(
            "derp.common.private_delivery.suppress_outbound_history"
        ) as suppress_history:
            await handle_successful_payment(message, sender, settlement)

        private = message.bot.send_message.await_args.kwargs
        assert private["chat_id"] == message.from_user.id
        assert private["protect_content"] is True
        assert "Purchased credits available: 135" in private["text"]
        assert "Used to repay payment debt: 30 credits" in private["text"]
        public = sender.reply.await_args.args[0]
        assert public == "I sent the payment details in a private chat."
        assert "135" not in public
        assert "30" not in public
        suppress_history.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_failed_group_private_delivery_is_durably_retried(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _payment_message(make_message, chat_type="supergroup")
        message.bot.send_message.side_effect = RuntimeError("private chat unavailable")
        sender = mock_sender(message=message)
        outcome = PaymentUpdateOutcome(
            inbox_id=UUID(int=70),
            kind=PaymentUpdateKind.SUCCESSFUL,
            disposition=PaymentUpdateDisposition.SETTLED,
            lease_token=UUID(int=71),
            fulfillment=FulfillmentResult(
                receipt_id=UUID(int=72),
                state=FulfillmentState.FULFILLED,
                available_credits=10,
            ),
        )
        inbox = MagicMock()
        inbox.reconcile = AsyncMock(return_value=outcome)
        inbox.finish_notification = AsyncMock()

        with patch("derp.common.private_delivery.report_exception"):
            await handle_successful_payment(
                message,
                sender,
                _settlement(),
                inbox,
                outcome.inbox_id,
            )

        inbox.finish_notification.assert_awaited_once_with(
            outcome,
            PaymentReplyDisposition.FAILED,
        )
        assert sender.reply.await_args.args[0].startswith("I couldn't send")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (
                FulfillmentResult(
                    receipt_id=UUID(int=50),
                    state=FulfillmentState.FULFILLED,
                    idempotent=True,
                ),
                "already applied",
            ),
            (
                FulfillmentResult(
                    receipt_id=UUID(int=51),
                    state=FulfillmentState.NEEDS_REVIEW,
                    review_reason="amount_mismatch",
                ),
                "need review",
            ),
            (
                FulfillmentResult(
                    receipt_id=UUID(int=52),
                    state=FulfillmentState.CLAWED_BACK,
                    idempotent=True,
                ),
                "already refunded",
            ),
        ],
    )
    async def test_settlement_state_has_unambiguous_user_copy(
        self,
        make_message,
        mock_sender,
        result,
        expected,
    ) -> None:
        message = _payment_message(make_message)
        sender = mock_sender(message=message)

        await handle_successful_payment(message, sender, _settlement(result))

        assert expected in sender.reply.await_args.args[0]

    @pytest.mark.asyncio
    async def test_inbox_replay_uses_durable_clawed_back_copy(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _payment_message(make_message)
        sender = mock_sender(message=message)
        outcome = PaymentUpdateOutcome(
            inbox_id=UUID(int=53),
            kind=PaymentUpdateKind.SUCCESSFUL,
            disposition=PaymentUpdateDisposition.SETTLED,
            lease_token=UUID(int=54),
            settlement_state=PaymentSettlementState.CLAWED_BACK,
        )
        inbox = MagicMock()
        inbox.reconcile = AsyncMock(return_value=outcome)
        inbox.finish_notification = AsyncMock()

        await handle_successful_payment(
            message,
            sender,
            _settlement(),
            inbox,
            outcome.inbox_id,
        )

        assert "already refunded" in sender.reply.await_args.args[0]
        inbox.finish_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fulfillment_failure_does_not_tell_user_to_buy_again(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _payment_message(make_message)
        sender = mock_sender(message=message)
        settlement = _settlement()
        settlement.fulfill.side_effect = RuntimeError("database unavailable")

        with patch("derp.handlers.payments.report_exception") as report:
            await handle_successful_payment(message, sender, settlement)

        report.assert_called_once()
        text = sender.reply.await_args.args[0]
        assert "needs review" in text
        assert "Don't buy again" in text

    @pytest.mark.asyncio
    async def test_missing_payment_returns_without_side_effects(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = make_message(text="")
        message.successful_payment = None
        sender = mock_sender(message=message)
        settlement = _settlement()

        await handle_successful_payment(message, sender, settlement)

        settlement.fulfill.assert_not_awaited()
        sender.reply.assert_not_awaited()


class TestRefundedPayment:
    @pytest.mark.asyncio
    async def test_maps_allowlisted_fields_and_reports_removed_value_and_debt(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _refund_message(make_message)
        sender = mock_sender(message=message)
        settlement = _refund_settlement(
            ClawbackResult(
                receipt_id=UUID(int=60),
                wallet_id=UUID(int=61),
                removed_available_credits=30,
                debt_created_credits=20,
                idempotent=False,
            )
        )

        await handle_refunded_payment(message, sender, settlement)

        settlement.clawback.assert_awaited_once_with(
            RefundedPaymentCommand(
                invoice_payload="dpi1_opaque-token",
                telegram_charge_id="telegram-refund-1",
                provider_charge_id=None,
                currency="XTR",
                total_amount=150,
            )
        )
        text = sender.reply.await_args.args[0]
        assert "Unused credits removed: 30" in text
        assert "Payment debt added: 20 credits" in text
        assert "Paid features are paused" in text

    @pytest.mark.asyncio
    async def test_group_refund_sends_amounts_only_to_protected_private_chat(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _refund_message(make_message, chat_type="supergroup")
        sender = mock_sender(message=message)
        settlement = _refund_settlement(
            ClawbackResult(
                receipt_id=UUID(int=64),
                wallet_id=UUID(int=65),
                removed_available_credits=30,
                debt_created_credits=20,
                idempotent=False,
            )
        )

        with patch(
            "derp.common.private_delivery.suppress_outbound_history"
        ) as suppress_history:
            await handle_refunded_payment(message, sender, settlement)

        private = message.bot.send_message.await_args.kwargs
        assert private["chat_id"] == message.from_user.id
        assert private["protect_content"] is True
        assert "Unused credits removed: 30" in private["text"]
        assert "Payment debt added: 20 credits" in private["text"]
        public = sender.reply.await_args.args[0]
        assert public == "I sent the refund details in a private chat."
        assert "30" not in public
        assert "20" not in public
        suppress_history.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_successful_group_private_delivery_completes_reply_lease(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _refund_message(make_message, chat_type="supergroup")
        sender = mock_sender(message=message)
        outcome = PaymentUpdateOutcome(
            inbox_id=UUID(int=73),
            kind=PaymentUpdateKind.REFUNDED,
            disposition=PaymentUpdateDisposition.SETTLED,
            lease_token=UUID(int=74),
            clawback=ClawbackResult(
                receipt_id=UUID(int=75),
                wallet_id=UUID(int=76),
                removed_available_credits=10,
                debt_created_credits=0,
                idempotent=False,
            ),
        )
        inbox = MagicMock()
        inbox.reconcile = AsyncMock(return_value=outcome)
        inbox.finish_notification = AsyncMock()

        await handle_refunded_payment(
            message,
            sender,
            _refund_settlement(),
            inbox,
            outcome.inbox_id,
        )

        inbox.finish_notification.assert_awaited_once_with(
            outcome,
            PaymentReplyDisposition.SENT,
        )

    @pytest.mark.asyncio
    async def test_idempotent_refund_has_unambiguous_copy(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _refund_message(
            make_message,
            provider_payment_charge_id="provider-refund-1",
        )
        sender = mock_sender(message=message)
        settlement = _refund_settlement(
            ClawbackResult(
                receipt_id=UUID(int=62),
                wallet_id=UUID(int=63),
                removed_available_credits=0,
                debt_created_credits=0,
                idempotent=True,
            )
        )

        await handle_refunded_payment(message, sender, settlement)

        settlement.clawback.assert_awaited_once_with(
            RefundedPaymentCommand(
                invoice_payload="dpi1_opaque-token",
                telegram_charge_id="telegram-refund-1",
                provider_charge_id="provider-refund-1",
                currency="XTR",
                total_amount=150,
            )
        )
        assert (
            sender.reply.await_args.args[0]
            == "This refund was already applied. No credits changed this time."
        )

    @pytest.mark.asyncio
    async def test_receipt_mismatch_reports_review_without_sensitive_log_fields(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _refund_message(make_message)
        sender = mock_sender(message=message)
        settlement = _refund_settlement()
        settlement.clawback.side_effect = PaymentConflictError("payload mismatch")

        with patch("derp.handlers.payments.logfire.warning") as warning:
            await handle_refunded_payment(message, sender, settlement)

        assert "needs review" in sender.reply.await_args.args[0]
        assert "No credits changed" in sender.reply.await_args.args[0]
        attributes = warning.call_args.kwargs
        assert attributes["error_type"] == "PaymentConflictError"
        assert attributes["charge_fingerprint"] != "telegram-refund-1"
        assert "invoice_payload" not in attributes

    @pytest.mark.asyncio
    async def test_reconciliation_failure_uses_redacted_exception_reporting(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = _refund_message(make_message)
        sender = mock_sender(message=message)
        settlement = _refund_settlement()
        error = RuntimeError("database endpoint with secret failed")
        settlement.clawback.side_effect = error

        with patch("derp.handlers.payments.report_exception") as report:
            await handle_refunded_payment(message, sender, settlement)

        report.assert_called_once_with(
            "payment_refund_reconciliation_failed",
            exception=error,
            charge_fingerprint=telemetry_fingerprint("telegram-refund-1"),
        )
        assert "needs review" in sender.reply.await_args.args[0]
        assert "No credits changed" in sender.reply.await_args.args[0]

    @pytest.mark.asyncio
    async def test_missing_refund_returns_without_side_effects(
        self,
        make_message,
        mock_sender,
    ) -> None:
        message = make_message(text="")
        message.refunded_payment = None
        sender = mock_sender(message=message)
        settlement = _refund_settlement()

        await handle_refunded_payment(message, sender, settlement)

        settlement.clawback.assert_not_awaited()
        sender.reply.assert_not_awaited()
