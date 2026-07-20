"""Personal Stars subscription status and renewal controls."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

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

from derp.billing import (
    SubscriptionManagementService,
    SubscriptionManagementSnapshot,
    SubscriptionStateError,
    SubscriptionStatus,
)
from derp.billing.telegram import TelegramSubscriptionRenewalProvider
from derp.common.sender import MessageSender
from derp.models import User as UserModel
from derp.observability import report_exception

router = Router(name="subscriptions")


class SubscriptionAction(StrEnum):
    """Authenticated-against-context subscription commands."""

    CANCEL = "cancel"
    RESUME = "resume"


class SubscriptionCallback(CallbackData, prefix="plan"):
    action: SubscriptionAction


def build_subscription_panel(
    snapshot: SubscriptionManagementSnapshot,
    *,
    now: datetime | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render honest renewal state without exposing provider identifiers."""
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    period_end = snapshot.current_period_end.astimezone(UTC).strftime(
        "%d %b %Y, %H:%M UTC"
    )
    expired = (
        snapshot.status is SubscriptionStatus.EXPIRED
        or snapshot.current_period_end <= observed_at
    )
    if expired:
        state = "Expired"
        timing = f"Last paid period ended {period_end}."
        markup = None
    elif snapshot.renewal_enabled:
        state = "Active · renews automatically"
        timing = f"Current allowance period ends {period_end}."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Cancel renewal",
                        callback_data=SubscriptionCallback(
                            action=SubscriptionAction.CANCEL
                        ).pack(),
                    )
                ]
            ]
        )
    else:
        state = "Active · renewal off"
        timing = f"Allowance remains available until {period_end}."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Re-enable renewal",
                        callback_data=SubscriptionCallback(
                            action=SubscriptionAction.RESUME
                        ).pack(),
                    )
                ]
            ]
        )
    text = f"<b>Derp Personal</b>\n{state}\n\n{timing}"
    return text, markup


@router.message(Command("plan", "subscription"))
async def show_subscription(
    message: Message,
    sender: MessageSender,
    subscription_management: SubscriptionManagementService,
    user_model: UserModel | None = None,
) -> Message:
    """Show the caller's current personal-plan renewal state."""
    if not user_model:
        return await message.reply("Could not find your account.")
    try:
        snapshot = await subscription_management.get_snapshot(user_model.id)
    except SubscriptionStateError:
        return await sender.reply(
            "<b>Derp Personal</b>\nYou do not have a personal plan."
        )
    text, markup = build_subscription_panel(snapshot)
    return await sender.reply(text, reply_markup=markup)


@router.callback_query(SubscriptionCallback.filter())
async def set_subscription_renewal(
    callback: CallbackQuery,
    callback_data: SubscriptionCallback,
    subscription_management: SubscriptionManagementService,
    user_model: UserModel | None = None,
) -> None:
    """Apply Telegram renewal state first, then render confirmed local state."""
    if not isinstance(callback.message, Message) or not user_model:
        return await callback.answer("Plan controls are unavailable", show_alert=True)
    if user_model.telegram_id != callback.from_user.id:
        return await callback.answer("Plan identity changed", show_alert=True)

    enabled = callback_data.action is SubscriptionAction.RESUME
    try:
        result = await subscription_management.set_renewal(
            user_model.id,
            enabled=enabled,
            provider=TelegramSubscriptionRenewalProvider(callback.bot),
        )
        snapshot = await subscription_management.get_snapshot(user_model.id)
    except SubscriptionStateError:
        return await callback.answer(
            "This plan changed or expired. Open /plan again.",
            show_alert=True,
        )
    except TelegramAPIError:
        report_exception(
            "subscription_provider_update_failed",
            level="warning",
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            "Telegram could not update renewal. Try again.",
            show_alert=True,
        )
    except Exception:
        report_exception(
            "subscription_local_update_failed",
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            "Renewal may have changed, but status needs reconciliation. Open /plan.",
            show_alert=True,
        )

    text, markup = build_subscription_panel(snapshot)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        report_exception(
            "subscription_panel_refresh_failed",
            level="warning",
            user_id=user_model.telegram_id,
        )
    notice = (
        "Renewal enabled"
        if result.renewal_enabled
        else "Renewal canceled; your paid period remains active"
    )
    await callback.answer(notice)


@router.callback_query(F.data.startswith("plan:"))
async def reject_stale_subscription_callback(callback: CallbackQuery) -> None:
    """Fail closed when an old plan button cannot be parsed."""
    await callback.answer(
        "This plan control expired. Open /plan again.", show_alert=True
    )


__all__ = [
    "SubscriptionAction",
    "SubscriptionCallback",
    "build_subscription_panel",
    "router",
]
