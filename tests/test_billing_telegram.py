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

    assert "your wallet" in text
    assert {callback.product_id for callback in callbacks} == {
        *DEFAULT_PRODUCT_CATALOG.current_top_ups,
        DEFAULT_PRODUCT_CATALOG.subscription_plan.id,
    }
    assert all(callback.target is PurchaseTargetCode.USER for callback in callbacks)
    assert sum(callback.kind is ProductKind.SUBSCRIPTION for callback in callbacks) == 1


def test_chat_panel_excludes_personal_subscription() -> None:
    text, markup = build_purchase_panel(target=PurchaseTargetCode.CHAT)
    callbacks = [
        PurchaseCallback.unpack(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "this chat" in text
    assert {callback.product_id for callback in callbacks} == set(
        DEFAULT_PRODUCT_CATALOG.current_top_ups
    )
    assert all(callback.kind is ProductKind.TOP_UP for callback in callbacks)
    assert all(callback.target is PurchaseTargetCode.CHAT for callback in callbacks)


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
