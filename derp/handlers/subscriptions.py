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
from aiogram.utils.i18n import gettext as _

from derp.billing import (
    SubscriptionManagementService,
    SubscriptionManagementSnapshot,
    SubscriptionStateError,
    SubscriptionStatus,
)
from derp.billing.telegram import TelegramSubscriptionRenewalProvider
from derp.common.private_delivery import deliver_sensitive_reply
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
        state = _("Expired")
        timing = _("Your last paid period ended {date}.").format(date=period_end)
        markup = None
    elif snapshot.renewal_enabled:
        state = _("Active · renews automatically")
        timing = _("Your current credits are available until {date}.").format(
            date=period_end
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Cancel automatic renewal"),
                        callback_data=SubscriptionCallback(
                            action=SubscriptionAction.CANCEL
                        ).pack(),
                    )
                ]
            ]
        )
    else:
        state = _("Active · renewal off")
        timing = _("Your credits remain available until {date}.").format(
            date=period_end
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Turn renewal back on"),
                        callback_data=SubscriptionCallback(
                            action=SubscriptionAction.RESUME
                        ).pack(),
                    )
                ]
            ]
        )
    text = _("<b>Derp Personal</b>\n{state}\n\n{timing}").format(
        state=state,
        timing=timing,
    )
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
        return await message.reply(_("I couldn't find your account. Try again."))
    try:
        snapshot = await subscription_management.get_snapshot(user_model.id)
    except SubscriptionStateError:
        text = _("<b>Derp Personal</b>\nYou don't have a monthly plan.")
        markup = None
    else:
        text, markup = build_subscription_panel(snapshot)
    return await deliver_sensitive_reply(
        message,
        sender,
        text,
        recipient_chat_id=user_model.telegram_id,
        public_success=_("I sent your plan details in a private chat."),
        public_failure=_(
            "I couldn't send your plan details. Open Derp privately and use /plan."
        ),
        failure_event="private_plan_delivery_failed",
        reply_markup=markup,
    )


@router.callback_query(SubscriptionCallback.filter())
async def set_subscription_renewal(
    callback: CallbackQuery,
    callback_data: SubscriptionCallback,
    subscription_management: SubscriptionManagementService,
    user_model: UserModel | None = None,
) -> None:
    """Apply Telegram renewal state first, then render confirmed local state."""
    if not isinstance(callback.message, Message) or not user_model:
        return await callback.answer(
            _("Plan controls are unavailable"), show_alert=True
        )
    if user_model.telegram_id != callback.from_user.id:
        return await callback.answer(
            _("These plan controls are no longer valid. Open /plan again."),
            show_alert=True,
        )
    if callback.message.chat.type != "private":
        return await callback.answer(
            _("Open Derp privately to manage your plan."),
            show_alert=True,
        )

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
            _("This plan changed or expired. Open /plan again."),
            show_alert=True,
        )
    except TelegramAPIError:
        report_exception(
            "subscription_provider_update_failed",
            level="warning",
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            _("Telegram couldn't update renewal. Try again."),
            show_alert=True,
        )
    except Exception:
        report_exception(
            "subscription_local_update_failed",
            user_id=user_model.telegram_id,
        )
        return await callback.answer(
            _("Renewal may have changed. Open /plan to check its current status."),
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
        _("Automatic renewal is on")
        if result.renewal_enabled
        else _("Automatic renewal is off. Your paid period stays active.")
    )
    await callback.answer(notice)


@router.callback_query(F.data.startswith("plan:"))
async def reject_stale_subscription_callback(callback: CallbackQuery) -> None:
    """Fail closed when an old plan button cannot be parsed."""
    await callback.answer(
        _("This plan control expired. Open /plan again."), show_alert=True
    )


__all__ = [
    "SubscriptionAction",
    "SubscriptionCallback",
    "build_subscription_panel",
    "router",
]
