"""Pure and boundary tests for native Telegram Stars presentation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from derp.billing import (
    ProductKind,
    PurchaseIntentHandle,
    PurchaseTarget,
    SubscriptionRenewalCommand,
)
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import (
    PurchaseCallback,
    PurchaseTargetCode,
    TelegramSubscriptionRenewalProvider,
    _credit_count,
    _day_count,
    _star_count,
    build_purchase_panel,
    create_stars_invoice_link,
)


def _handle(kind: ProductKind) -> PurchaseIntentHandle:
    product = (
        DEFAULT_PRODUCT_CATALOG.subscription_plan
        if kind is ProductKind.SUBSCRIPTION
        else DEFAULT_PRODUCT_CATALOG.current_top_ups["starter"]
    )
    return PurchaseIntentHandle(
        intent_id=UUID(int=1),
        invoice_payload="dpi1_opaque-token",
        product_kind=kind,
        product_id=product.id,
        product_version=product.version,
        target=PurchaseTarget.user(UUID(int=2)),
        credits=product.credits,
        stars=product.stars,
        currency=product.currency,
        expires_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        subscription_period_seconds=getattr(product, "period_seconds", None),
    )


def test_personal_panel_uses_typed_callbacks_for_current_catalog() -> None:
    text, markup = build_purchase_panel(target=PurchaseTargetCode.USER)
    callbacks = [
        PurchaseCallback.unpack(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]

    assert text == (
        "<b>Buy personal credits</b>\n"
        "Choose an option. Telegram asks you to confirm before charging."
    )
    assert {callback.product_id for callback in callbacks} == {
        *DEFAULT_PRODUCT_CATALOG.current_top_ups,
        DEFAULT_PRODUCT_CATALOG.subscription_plan.id,
    }
    assert all(callback.target is PurchaseTargetCode.USER for callback in callbacks)
    assert sum(callback.kind is ProductKind.SUBSCRIPTION for callback in callbacks) == 1
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "600 credits · 50 Stars" in labels
    assert "Derp Personal · 500 Stars / 30 days" in labels


def test_chat_panel_excludes_personal_subscription() -> None:
    text, markup = build_purchase_panel(target=PurchaseTargetCode.CHAT)
    callbacks = [
        PurchaseCallback.unpack(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]

    assert text == (
        "<b>Buy chat credits</b>\n"
        "Choose an option. Telegram asks you to confirm before charging."
    )
    assert {callback.product_id for callback in callbacks} == set(
        DEFAULT_PRODUCT_CATALOG.current_top_ups
    )
    assert all(callback.kind is ProductKind.TOP_UP for callback in callbacks)
    assert all(callback.target is PurchaseTargetCode.CHAT for callback in callbacks)


@pytest.mark.parametrize(
    ("count", "credits", "stars", "days"),
    [
        (1, "1 credit", "1 Star", "1 day"),
        (2, "2 credits", "2 Stars", "2 days"),
    ],
)
def test_invoice_quantities_use_gettext_plurals(
    count: int,
    credits: str,
    stars: str,
    days: str,
) -> None:
    assert _credit_count(count) == credits
    assert _star_count(count) == stars
    assert _day_count(count) == days


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_title", "expected_period"),
    [
        (ProductKind.TOP_UP, "Derp credits", None),
        (ProductKind.SUBSCRIPTION, "Derp Personal", 30 * 24 * 60 * 60),
    ],
)
async def test_invoice_link_uses_exact_immutable_intent_terms(
    kind: ProductKind,
    expected_title: str,
    expected_period: int | None,
) -> None:
    handle = _handle(kind)
    bot = MagicMock()
    bot.create_invoice_link = AsyncMock(return_value="https://t.me/$invoice")

    result = await create_stars_invoice_link(
        bot,
        handle,
        business_connection_id="business-1",
    )

    assert result == "https://t.me/$invoice"
    bot.create_invoice_link.assert_awaited_once()
    values = bot.create_invoice_link.await_args.kwargs
    assert values["title"] == expected_title
    assert values["payload"] == handle.invoice_payload
    assert values["currency"] == handle.currency == "XTR"
    assert values["provider_token"] == ""
    assert values["subscription_period"] == expected_period
    assert values["business_connection_id"] == "business-1"
    assert len(values["prices"]) == 1
    assert values["prices"][0].label == expected_title
    assert values["prices"][0].amount == handle.stars
    if kind is ProductKind.SUBSCRIPTION:
        assert values["description"] == (
            "6750 credits every 30 days for 500 Stars. "
            "Renews automatically until canceled. "
            "Telegram asks you to confirm the first charge."
        )
    else:
        assert values["description"] == (
            "600 credits for your account. One-time price: 50 Stars. "
            "Telegram charges you only after you confirm."
        )


@pytest.mark.asyncio
async def test_chat_invoice_names_the_exact_credit_target() -> None:
    product = DEFAULT_PRODUCT_CATALOG.current_top_ups["starter"]
    handle = PurchaseIntentHandle(
        intent_id=UUID(int=3),
        invoice_payload="dpi1_chat-token",
        product_kind=ProductKind.TOP_UP,
        product_id=product.id,
        product_version=product.version,
        target=PurchaseTarget.chat(UUID(int=4)),
        credits=product.credits,
        stars=product.stars,
        currency=product.currency,
        expires_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        subscription_period_seconds=None,
    )
    bot = MagicMock()
    bot.create_invoice_link = AsyncMock(return_value="https://t.me/$invoice")

    await create_stars_invoice_link(bot, handle)

    assert bot.create_invoice_link.await_args.kwargs["description"] == (
        "600 credits for this chat. One-time price: 50 Stars. "
        "Telegram charges you only after you confirm."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_subscription_provider_maps_renewal_to_telegram_cancellation(
    enabled: bool,
) -> None:
    bot = MagicMock()
    bot.edit_user_star_subscription = AsyncMock(return_value=True)
    provider = TelegramSubscriptionRenewalProvider(bot)

    await provider.set_renewal(
        SubscriptionRenewalCommand(
            payer_telegram_id=12345,
            telegram_payment_charge_id="subscription-charge",
            enabled=enabled,
        )
    )

    bot.edit_user_star_subscription.assert_awaited_once_with(
        user_id=12345,
        telegram_payment_charge_id="subscription-charge",
        is_canceled=not enabled,
    )
