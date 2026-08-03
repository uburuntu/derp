"""Shared Telegram presentation for durable support-case receipts."""

from __future__ import annotations

from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _

from derp.support.types import (
    OperatorSupportCase,
    SupportCase,
    SupportKind,
    SupportStatus,
)


class SupportStatusCallback(CallbackData, prefix="support-status"):
    """Requester-bound refresh for one stable case message."""

    reference: str
    actor_id: int


def support_kind_label(kind: SupportKind) -> str:
    """Return one localized category label shared by both case surfaces."""
    return {
        SupportKind.PAYMENT: _("Payment or credits"),
        SupportKind.REFUND: _("Refund"),
        SupportKind.PRIVACY: _("Privacy or data"),
        SupportKind.ACCESS: _("Access or account"),
    }[kind]


def build_support_status_receipt(
    case: SupportCase | OperatorSupportCase,
    *,
    actor_telegram_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render current database state into the case's one stable message."""
    title = {
        SupportStatus.OPEN: _("Support case open"),
        SupportStatus.REFUND_PENDING: _("Refund requested"),
        SupportStatus.RESOLVED: _("Support case resolved"),
        SupportStatus.DECLINED: _("Support case declined"),
    }[case.status]
    detail = case.decision_reason or (
        _("I have your note. An operator will review it here.")
        if case.status is SupportStatus.OPEN
        else _("The case status changed. Refresh /support if you need help again.")
    )
    text = _("<b>{title}</b>\n{kind}: <code>{reference}</code>\n\n{detail}").format(
        title=title,
        kind=escape(support_kind_label(case.kind)),
        reference=escape(case.reference),
        detail=escape(detail),
    )
    if case.status not in {SupportStatus.OPEN, SupportStatus.REFUND_PENDING}:
        return text, None
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Refresh"),
                    callback_data=SupportStatusCallback(
                        reference=case.reference,
                        actor_id=actor_telegram_id,
                    ).pack(),
                )
            ]
        ]
    )
    return text, markup


__all__ = [
    "SupportStatusCallback",
    "build_support_status_receipt",
    "support_kind_label",
]
