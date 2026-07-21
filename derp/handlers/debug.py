"""Operator-only durable one-Star purchase validation."""

from __future__ import annotations

from typing import Final

import logfire
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import gettext as _

from derp.billing import (
    DEFAULT_PRODUCT_CATALOG,
    PurchaseIntentService,
    PurchaseTarget,
)
from derp.billing.telegram import PurchaseTargetCode, create_stars_invoice_link
from derp.common.sender import MessageSender
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operator import OperatorOnlyFilter

DEBUG_PURCHASE_COMMANDS: Final = ("debug_buy", "dbuy")
RETIRED_DEBUG_COMMANDS: Final = (
    "debug_credits",
    "dcredits",
    "debug_reset",
    "dreset",
    "debug_refund",
    "drefund",
    "debug_status",
    "dstatus",
    "debug_tools",
    "dtools",
    "debug_help",
    "dhelp",
)
ALL_DEBUG_COMMANDS: Final = (*DEBUG_PURCHASE_COMMANDS, *RETIRED_DEBUG_COMMANDS)

router = Router(name="debug")
router.message.filter(OperatorOnlyFilter())
router.callback_query.filter(OperatorOnlyFilter())

# This router must immediately follow ``router`` so unauthorized control input
# is consumed before the conversational catch-all.
rejection_router = Router(name="debug_rejection")


class DebugPurchaseCallback(CallbackData, prefix="debug-buy"):
    """Operator-only selector backed by immutable catalog terms."""

    product_id: str
    target: PurchaseTargetCode


@router.message(Command(*DEBUG_PURCHASE_COMMANDS))
async def debug_buy_command(
    message: Message,
    sender: MessageSender,
) -> Message:
    """Present the hidden one-Star product for safe live Stars validation."""
    sender.protect_content = True
    if (
        message.chat.type != "private"
        or message.from_user is None
        or message.chat.id != message.from_user.id
    ):
        return await sender.reply(
            _("Operator controls are private. Open /operator in your private chat.")
        )
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up
    rows = [
        [
            InlineKeyboardButton(
                text=_("Personal wallet · {stars} Star").format(stars=product.stars),
                callback_data=DebugPurchaseCallback(
                    product_id=product.id,
                    target=PurchaseTargetCode.USER,
                ).pack(),
            )
        ]
    ]
    return await sender.reply(
        _(
            "<b>Live Stars test</b>\n"
            "{credits} test credits for {stars} Star. Uses the production "
            "purchase and settlement flow."
        ).format(credits=product.credits, stars=product.stars),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(DebugPurchaseCallback.filter())
async def handle_debug_buy_callback(
    callback: CallbackQuery,
    callback_data: DebugPurchaseCallback,
    purchase_intents: PurchaseIntentService,
    user_model: UserModel | None = None,
) -> None:
    """Create an opaque durable intent for the selected live wallet target."""
    product = DEFAULT_PRODUCT_CATALOG.debug_top_up
    if not isinstance(callback.message, Message) or not user_model:
        return await callback.answer(_("Stars test unavailable"), show_alert=True)
    if (
        callback.message.chat.type != "private"
        or callback.message.chat.id != callback.from_user.id
    ):
        return await callback.answer(
            _("Operator controls are private. Open /operator in your private chat."),
            show_alert=True,
        )
    if user_model.telegram_id != callback.from_user.id:
        return await callback.answer(
            _("Stars test identity changed"),
            show_alert=True,
        )
    if callback_data.product_id != product.id:
        return await callback.answer(
            _("This option expired. Run /debug_buy again."),
            show_alert=True,
        )

    if callback_data.target is PurchaseTargetCode.CHAT:
        return await callback.answer(
            _("This chat is unavailable"),
            show_alert=True,
        )
    target = PurchaseTarget.user(user_model.id)

    try:
        handle = await purchase_intents.create_operator_debug_top_up_intent(
            payer_user_id=user_model.id,
            target=target,
        )
        invoice_link = await create_stars_invoice_link(
            callback.bot,
            handle,
            business_connection_id=callback.message.business_connection_id,
        )
    except LookupError, ValueError:
        return await callback.answer(
            _("This Stars test is no longer available"),
            show_alert=True,
        )
    except TelegramAPIError as exc:
        report_exception(
            "operator.debug_purchase_invoice_link_failed",
            exception=exc,
            level="warning",
            product_id=product.id,
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            _("Telegram could not prepare the invoice. Try again."),
            show_alert=True,
        )

    owner = _("this chat") if target.kind.value == "chat" else _("your wallet")
    await callback.message.answer(
        _(
            "<b>{credits} test credits for {owner}</b>\n"
            "Telegram shows the final {stars}-Star confirmation before charging."
        ).format(
            credits=handle.credits,
            owner=owner,
            stars=handle.stars,
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Pay {stars} Star").format(stars=handle.stars),
                        url=invoice_link,
                    )
                ]
            ]
        ),
        protect_content=True,
    )
    await callback.answer(_("Invoice ready"))
    logfire.info(
        "operator.debug_purchase_intent_presented",
        intent_id=str(handle.intent_id),
        product_id=handle.product_id,
        product_version=handle.product_version,
        target=target.kind.value,
        user_id=user_model.telegram_id,
    )


@router.callback_query(F.data.startswith("dbuy:"))
async def reject_legacy_debug_buy_callback(callback: CallbackQuery) -> None:
    """Fail closed for legacy selectors that embedded commercial pack data."""
    await callback.answer(
        _("This option expired. Run /debug_buy again."),
        show_alert=True,
    )


@router.message(Command(*RETIRED_DEBUG_COMMANDS))
async def retired_debug_command(
    _message: Message,
    sender: MessageSender,
) -> Message:
    """Redirect retired, non-authoritative controls to the operator console."""
    return await sender.reply(_("Use /operator for diagnostics and controls."))


@rejection_router.message(Command(*ALL_DEBUG_COMMANDS))
async def reject_unauthorized_debug_command(_message: Message) -> None:
    """Consume unauthorized debug commands without disclosing operator access."""


@rejection_router.callback_query(F.data.startswith("debug-buy:"))
@rejection_router.callback_query(F.data.startswith("dbuy:"))
async def reject_unauthorized_debug_callback(callback: CallbackQuery) -> None:
    """Clear unauthorized or malformed debug callback spinners."""
    await callback.answer(_("This action is unavailable"), show_alert=True)


__all__ = [
    "ALL_DEBUG_COMMANDS",
    "DEBUG_PURCHASE_COMMANDS",
    "RETIRED_DEBUG_COMMANDS",
    "DebugPurchaseCallback",
    "debug_buy_command",
    "handle_debug_buy_callback",
    "rejection_router",
    "reject_legacy_debug_buy_callback",
    "reject_unauthorized_debug_callback",
    "reject_unauthorized_debug_command",
    "retired_debug_command",
    "router",
]
