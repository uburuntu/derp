"""Native Telegram presentation values for durable Stars products."""

from __future__ import annotations

from enum import StrEnum

from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from aiogram.utils.i18n import gettext as _

from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.types import (
    ProductKind,
    PurchaseIntentHandle,
    SubscriptionRenewalCommand,
)


class PurchaseTargetCode(StrEnum):
    """Compact callback target resolved against live handler context."""

    USER = "user"
    CHAT = "chat"


class PurchaseCallback(CallbackData, prefix="buy"):
    """Non-commercial selector; exact terms live only in the intent table."""

    kind: ProductKind
    product_id: str
    target: PurchaseTargetCode
    actor_id: int = 0


class PurchaseTargetCallback(CallbackData, prefix="buy-target"):
    """Bind a contextual group purchase target to the user who opened it."""

    target: PurchaseTargetCode
    actor_id: int


class PurchaseTermsAcceptCallback(CallbackData, prefix="bt"):
    """Carry one exact catalog selection through the purchase Terms gate."""

    version: str
    kind: ProductKind
    product_id: str
    target: PurchaseTargetCode
    actor_id: int

    def purchase(self) -> PurchaseCallback:
        """Recover the exact non-commercial selector accepted by the actor."""
        return PurchaseCallback(
            kind=self.kind,
            product_id=self.product_id,
            target=self.target,
            actor_id=self.actor_id,
        )


class TelegramSubscriptionRenewalProvider:
    """Apply subscription renewal state through Telegram's Stars API."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def set_renewal(self, command: SubscriptionRenewalCommand) -> None:
        """Cancel or re-enable extension without shortening the paid period."""
        confirmed = await self._bot.edit_user_star_subscription(
            user_id=command.payer_telegram_id,
            telegram_payment_charge_id=command.telegram_payment_charge_id,
            is_canceled=not command.enabled,
        )
        if confirmed is not True:
            raise RuntimeError("Telegram did not confirm the renewal change")


def _credit_count(count: int) -> str:
    return _("{count} credit", "{count} credits", count).format(count=count)


def _star_count(count: int) -> str:
    return _("{count} Star", "{count} Stars", count).format(count=count)


def _day_count(count: int) -> str:
    return _("{count} day", "{count} days", count).format(count=count)


def build_purchase_panel(
    *,
    target: PurchaseTargetCode,
    actor_telegram_id: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a compact top-up picker, with the personal plan only for users."""
    rows: list[list[InlineKeyboardButton]] = []
    top_ups = tuple(DEFAULT_PRODUCT_CATALOG.current_top_ups.values())
    for offset in range(0, len(top_ups), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("{credits} · {stars}").format(
                        credits=_credit_count(product.credits),
                        stars=_star_count(product.stars),
                    ),
                    callback_data=PurchaseCallback(
                        kind=ProductKind.TOP_UP,
                        product_id=product.id,
                        target=target,
                        actor_id=actor_telegram_id,
                    ).pack(),
                )
                for product in top_ups[offset : offset + 2]
            ]
        )
    if target is PurchaseTargetCode.USER:
        plan = DEFAULT_PRODUCT_CATALOG.subscription_plan
        period_days = plan.period_seconds // (24 * 60 * 60)
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("{plan} · {stars} / {days}").format(
                        plan=_("Derp Personal"),
                        stars=_star_count(plan.stars),
                        days=_day_count(period_days),
                    ),
                    callback_data=PurchaseCallback(
                        kind=ProductKind.SUBSCRIPTION,
                        product_id=plan.id,
                        target=target,
                        actor_id=actor_telegram_id,
                    ).pack(),
                )
            ]
        )
    heading = (
        _("Buy chat credits")
        if target is PurchaseTargetCode.CHAT
        else _("Buy personal credits")
    )
    text = f"<b>{heading}</b>\n" + _(
        "Choose an option. Telegram asks you to confirm before charging."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_purchase_target_panel(
    *,
    actor_telegram_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Ask one group actor whether the purchase is personal or shared."""
    if actor_telegram_id <= 0:
        raise ValueError("actor_telegram_id must be positive")
    return (
        _("<b>Buy credits</b>\nWho are they for?"),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("For me"),
                        callback_data=PurchaseTargetCallback(
                            target=PurchaseTargetCode.USER,
                            actor_id=actor_telegram_id,
                        ).pack(),
                    ),
                    InlineKeyboardButton(
                        text=_("For this chat"),
                        callback_data=PurchaseTargetCallback(
                            target=PurchaseTargetCode.CHAT,
                            actor_id=actor_telegram_id,
                        ).pack(),
                    ),
                ]
            ]
        ),
    )


async def create_stars_invoice_link(
    bot: Bot,
    handle: PurchaseIntentHandle,
    *,
    business_connection_id: str | None = None,
) -> str:
    """Create one Telegram Stars invoice from immutable intent terms."""
    title = (
        _("Derp Personal")
        if handle.product_kind is ProductKind.SUBSCRIPTION
        else _("Derp credits")
    )
    credit_text = _credit_count(handle.credits)
    star_text = _star_count(handle.stars)
    if handle.product_kind is ProductKind.SUBSCRIPTION:
        if handle.subscription_period_seconds is None:
            raise ValueError("subscription invoice requires a billing period")
        period_days = handle.subscription_period_seconds // (24 * 60 * 60)
        description = _(
            "{credits} every {days} for {stars}. Renews automatically until canceled. "
            "Telegram asks you to confirm the first charge."
        ).format(
            credits=credit_text,
            days=_day_count(period_days),
            stars=star_text,
        )
    elif handle.target.kind.value == "chat":
        description = _(
            "{credits} for this chat. One-time price: {stars}. "
            "Telegram charges you only after you confirm."
        ).format(credits=credit_text, stars=star_text)
    else:
        description = _(
            "{credits} for your account. One-time price: {stars}. "
            "Telegram charges you only after you confirm."
        ).format(credits=credit_text, stars=star_text)
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
    "PurchaseTargetCallback",
    "PurchaseTargetCode",
    "PurchaseTermsAcceptCallback",
    "TelegramSubscriptionRenewalProvider",
    "build_purchase_panel",
    "build_purchase_target_panel",
    "create_stars_invoice_link",
]
