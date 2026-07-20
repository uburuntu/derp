"""Native Telegram presentation values for durable Stars products."""

from __future__ import annotations

from enum import StrEnum

from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.types import ProductKind, PurchaseIntentHandle


class PurchaseTargetCode(StrEnum):
    """Compact callback target resolved against live handler context."""

    USER = "user"
    CHAT = "chat"


class PurchaseCallback(CallbackData, prefix="buy"):
    """Non-commercial selector; exact terms live only in the intent table."""

    kind: ProductKind
    product_id: str
    target: PurchaseTargetCode


def build_purchase_panel(
    *,
    target: PurchaseTargetCode,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a compact top-up picker, with the personal plan only for users."""
    rows: list[list[InlineKeyboardButton]] = []
    top_ups = tuple(DEFAULT_PRODUCT_CATALOG.current_top_ups.values())
    for offset in range(0, len(top_ups), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{product.credits} credits · {product.stars} Stars",
                    callback_data=PurchaseCallback(
                        kind=ProductKind.TOP_UP,
                        product_id=product.id,
                        target=target,
                    ).pack(),
                )
                for product in top_ups[offset : offset + 2]
            ]
        )
    if target is PurchaseTargetCode.USER:
        plan = DEFAULT_PRODUCT_CATALOG.subscription_plan
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.name} · {plan.stars} Stars / 30 days",
                    callback_data=PurchaseCallback(
                        kind=ProductKind.SUBSCRIPTION,
                        product_id=plan.id,
                        target=target,
                    ).pack(),
                )
            ]
        )
    owner = "this chat" if target is PurchaseTargetCode.CHAT else "your wallet"
    text = (
        f"<b>Add credits to {owner}</b>\n"
        "Choose an exact Stars price. Telegram confirms the purchase before "
        "anything is added."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def create_stars_invoice_link(
    bot: Bot,
    handle: PurchaseIntentHandle,
    *,
    business_connection_id: str | None = None,
) -> str:
    """Create one Telegram Stars invoice from immutable intent terms."""
    title = (
        "Derp Personal"
        if handle.product_kind is ProductKind.SUBSCRIPTION
        else "Derp credits"
    )
    target = "shared chat" if handle.target.kind.value == "chat" else "personal"
    description = (
        f"{handle.credits} credits for the {target} wallet. "
        "Charges apply only after Telegram confirmation."
    )
    return await bot.create_invoice_link(
        title=title,
        description=description,
        payload=handle.invoice_payload,
        currency=handle.currency,
        prices=[LabeledPrice(label=title, amount=handle.stars)],
        provider_token="",
        subscription_period=handle.subscription_period_seconds,
        business_connection_id=business_connection_id,
    )


__all__ = [
    "PurchaseCallback",
    "PurchaseTargetCode",
    "build_purchase_panel",
    "create_stars_invoice_link",
]
