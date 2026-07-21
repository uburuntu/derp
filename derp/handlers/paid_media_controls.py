"""Shared Telegram controls for paid-media approvals and funding."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _

from derp.approvals.paid_media import (
    PaidMediaApprovalAction,
    PaidMediaApprovalKind,
    pack_paid_media_callback,
)
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseCallback, PurchaseTargetCode


@dataclass(frozen=True, slots=True)
class PaidMediaPurchaseControl:
    """One policy-selected wallet top-up rendered as a native callback."""

    target: PurchaseTargetCode
    callback_data: str


def paid_media_decision_keyboard(
    kind: PaidMediaApprovalKind,
    token: str,
) -> InlineKeyboardMarkup:
    """Present the exact Run/Cancel decision for one quoted operation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Run"),
                    callback_data=pack_paid_media_callback(
                        kind,
                        PaidMediaApprovalAction.RUN,
                        token,
                    ),
                ),
                InlineKeyboardButton(
                    text=_("Cancel"),
                    callback_data=pack_paid_media_callback(
                        kind,
                        PaidMediaApprovalAction.CANCEL,
                        token,
                    ),
                ),
            ]
        ]
    )


def paid_media_retry_keyboard(
    kind: PaidMediaApprovalKind,
    token: str,
) -> InlineKeyboardMarkup:
    """Retry an approved lease without creating another operation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Run"),
                    callback_data=pack_paid_media_callback(
                        kind,
                        PaidMediaApprovalAction.RUN,
                        token,
                    ),
                )
            ]
        ]
    )


def paid_media_funding_keyboard(
    kind: PaidMediaApprovalKind,
    token: str,
    *,
    personal_once: bool,
    purchase: PaidMediaPurchaseControl | None,
) -> InlineKeyboardMarkup:
    """Offer explicit personal consent and chat funding without silent fallback."""
    rows: list[list[InlineKeyboardButton]] = []
    if personal_once:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("Use mine once"),
                    callback_data=pack_paid_media_callback(
                        kind,
                        PaidMediaApprovalAction.USE_PERSONAL_ONCE,
                        token,
                    ),
                ),
                InlineKeyboardButton(
                    text=_("Always here"),
                    callback_data=pack_paid_media_callback(
                        kind,
                        PaidMediaApprovalAction.ALWAYS_HERE,
                        token,
                    ),
                ),
            ]
        )

    if purchase is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        _("Buy for chat")
                        if purchase.target is PurchaseTargetCode.CHAT
                        else _("Buy credits")
                    ),
                    callback_data=purchase.callback_data,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Run"),
                callback_data=pack_paid_media_callback(
                    kind,
                    PaidMediaApprovalAction.RUN,
                    token,
                ),
            ),
            InlineKeyboardButton(
                text=_("Cancel"),
                callback_data=pack_paid_media_callback(
                    kind,
                    PaidMediaApprovalAction.CANCEL,
                    token,
                ),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def paid_media_purchase_control(
    required_credits: int,
    target: PurchaseTargetCode,
) -> PaidMediaPurchaseControl | None:
    """Select the smallest current top-up for the policy-approved wallet."""
    if not isinstance(target, PurchaseTargetCode):
        raise TypeError("target must be a PurchaseTargetCode")
    eligible = (
        product
        for product in DEFAULT_PRODUCT_CATALOG.current_top_ups.values()
        if product.credits >= required_credits
    )
    product = min(eligible, key=lambda item: (item.credits, item.stars), default=None)
    if product is None:
        return None
    packed = PurchaseCallback(
        kind=product.kind,
        product_id=product.id,
        target=target,
    ).pack()
    if len(packed.encode("utf-8")) > 64:
        raise ValueError("chat purchase callback exceeds Telegram's 64-byte limit")
    return PaidMediaPurchaseControl(target, packed)


__all__ = [
    "PaidMediaPurchaseControl",
    "paid_media_decision_keyboard",
    "paid_media_funding_keyboard",
    "paid_media_purchase_control",
    "paid_media_retry_keyboard",
]
