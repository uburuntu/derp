"""Tests for personal-plan Telegram controls."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram.types import CallbackQuery

from derp.billing import (
    SubscriptionManagementSnapshot,
    SubscriptionStateError,
    SubscriptionStateResult,
    SubscriptionStatus,
)
from derp.billing.telegram import TelegramSubscriptionRenewalProvider
from derp.handlers.subscriptions import (
    SubscriptionAction,
    SubscriptionCallback,
    build_subscription_panel,
    reject_stale_subscription_callback,
    set_subscription_renewal,
    show_subscription,
)

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _snapshot(
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    renewal_enabled: bool = True,
    period_end: datetime | None = None,
) -> SubscriptionManagementSnapshot:
    return SubscriptionManagementSnapshot(
        subscription_id=UUID(int=1),
        user_id=UUID(int=2),
        payer_telegram_id=12345,
        plan_id="personal_monthly",
        plan_version="v1",
        status=status,
        renewal_enabled=renewal_enabled,
        current_period_end=period_end or NOW + timedelta(days=30),
        telegram_payment_charge_id="private-charge-id",
    )


def _management(snapshot: SubscriptionManagementSnapshot) -> MagicMock:
    service = MagicMock()
    service.get_snapshot = AsyncMock(return_value=snapshot)
    service.set_renewal = AsyncMock(
        return_value=SubscriptionStateResult(
            snapshot.subscription_id,
            snapshot.renewal_enabled,
            snapshot.current_period_end,
            True,
        )
    )
    return service


def test_panel_shows_cancel_for_active_renewal_without_charge_id() -> None:
    text, markup = build_subscription_panel(_snapshot(), now=NOW)

    assert "renews automatically" in text
    assert "19 Aug 2026, 12:00 UTC" in text
    assert "private-charge-id" not in text
    assert markup is not None
    callback = SubscriptionCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert callback.action is SubscriptionAction.CANCEL


def test_panel_localizes_utc_date_in_russian(setup_i18n) -> None:
    with setup_i18n.use_locale("ru"):
        text, _ = build_subscription_panel(_snapshot(), now=NOW)

    assert "19 авг. 2026, 12:00 UTC" in text


def test_panel_keeps_paid_period_when_renewal_is_off() -> None:
    text, markup = build_subscription_panel(
        _snapshot(status=SubscriptionStatus.CANCELED, renewal_enabled=False),
        now=NOW,
    )

    assert "renewal off" in text
    assert "remain available" in text
    assert markup is not None
    callback = SubscriptionCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert callback.action is SubscriptionAction.RESUME


def test_expired_panel_has_no_action() -> None:
    text, markup = build_subscription_panel(
        _snapshot(
            status=SubscriptionStatus.EXPIRED,
            renewal_enabled=False,
            period_end=NOW - timedelta(seconds=1),
        ),
        now=NOW,
    )

    assert "Expired" in text
    assert markup is None


@pytest.mark.asyncio
async def test_show_subscription_renders_current_state(
    make_message,
    mock_sender,
    mock_user_model,
) -> None:
    message = make_message(text="/plan", chat_type="private", chat_id=12345)
    sender = mock_sender(message=message)
    user = mock_user_model(user_id=UUID(int=2))
    service = _management(_snapshot())

    await show_subscription(message, sender, service, user)

    service.get_snapshot.assert_awaited_once_with(user.id)
    assert "Derp Personal" in sender.reply.await_args.args[0]
    assert sender.reply.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_group_plan_sends_sensitive_state_only_to_protected_private_chat(
    make_message,
    mock_sender,
    mock_user_model,
) -> None:
    message = make_message(text="/plan", chat_type="supergroup")
    sender = mock_sender(message=message)
    user = mock_user_model(user_id=UUID(int=2), telegram_id=12345)

    with patch(
        "derp.common.private_delivery.suppress_outbound_history"
    ) as suppress_history:
        await show_subscription(message, sender, _management(_snapshot()), user)

    private = message.bot.send_message.await_args.kwargs
    assert private["chat_id"] == user.telegram_id
    assert private["protect_content"] is True
    assert "Derp Personal" in private["text"]
    assert private["reply_markup"] is not None
    public = sender.reply.await_args.args[0]
    assert public == "I sent your plan details in a private chat."
    assert "renews" not in public
    assert "2026" not in public
    suppress_history.assert_called_once_with()


@pytest.mark.asyncio
async def test_group_plan_dm_failure_remains_content_free(
    make_message,
    mock_sender,
    mock_user_model,
) -> None:
    message = make_message(text="/plan", chat_type="supergroup")
    message.bot.send_message.side_effect = RuntimeError("blocked private chat")
    sender = mock_sender(message=message)
    user = mock_user_model(user_id=UUID(int=2), telegram_id=12345)

    with patch("derp.common.private_delivery.report_exception") as report:
        await show_subscription(message, sender, _management(_snapshot()), user)

    public = sender.reply.await_args.args[0]
    assert "couldn't send" in public
    assert "renews" not in public
    assert "2026" not in public
    report.assert_called_once()


@pytest.mark.asyncio
async def test_show_subscription_handles_missing_plan(
    make_message,
    mock_sender,
    mock_user_model,
) -> None:
    message = make_message(text="/plan", chat_type="private", chat_id=12345)
    sender = mock_sender(message=message)
    service = _management(_snapshot())
    service.get_snapshot.side_effect = SubscriptionStateError("missing")

    await show_subscription(message, sender, service, mock_user_model())

    assert "don't have" in sender.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_callback_uses_context_actor_and_refreshes_panel(
    make_message,
    make_user,
    mock_user_model,
) -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = make_message(text="plan", chat_type="private", chat_id=12345)
    callback.message.edit_text = AsyncMock()
    callback.from_user = make_user(id=12345)
    callback.bot = MagicMock()
    callback.answer = AsyncMock()
    user = mock_user_model(user_id=UUID(int=2), telegram_id=12345)
    snapshot = _snapshot(
        status=SubscriptionStatus.CANCELED,
        renewal_enabled=False,
    )
    service = _management(snapshot)

    await set_subscription_renewal(
        callback,
        SubscriptionCallback(action=SubscriptionAction.CANCEL),
        service,
        user,
    )

    call = service.set_renewal.await_args
    assert call.args == (user.id,)
    assert call.kwargs["enabled"] is False
    assert isinstance(call.kwargs["provider"], TelegramSubscriptionRenewalProvider)
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once_with(
        "Automatic renewal is off. Your paid period stays active."
    )


@pytest.mark.asyncio
async def test_group_plan_control_fails_closed_before_provider_io(
    make_message,
    make_user,
    mock_user_model,
) -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = make_message(text="plan", chat_type="supergroup")
    callback.from_user = make_user(id=12345)
    callback.answer = AsyncMock()
    service = _management(_snapshot())

    await set_subscription_renewal(
        callback,
        SubscriptionCallback(action=SubscriptionAction.CANCEL),
        service,
        mock_user_model(user_id=UUID(int=2), telegram_id=12345),
    )

    service.set_renewal.assert_not_awaited()
    service.get_snapshot.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "Open Derp privately to manage your plan.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_callback_rejects_changed_actor_before_provider_io(
    make_message,
    make_user,
    mock_user_model,
) -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = make_message(text="plan")
    callback.from_user = make_user(id=999)
    callback.answer = AsyncMock()
    service = _management(_snapshot())

    await set_subscription_renewal(
        callback,
        SubscriptionCallback(action=SubscriptionAction.CANCEL),
        service,
        mock_user_model(telegram_id=12345),
    )

    service.set_renewal.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "This plan link is no longer valid. Open /plan again.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_callback_reports_when_plan_cannot_be_managed() -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = None
    callback.answer = AsyncMock()
    service = MagicMock()

    await set_subscription_renewal(
        callback,
        SubscriptionCallback(action=SubscriptionAction.CANCEL),
        service,
    )

    callback.answer.assert_awaited_once_with(
        "I can't manage this plan right now",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_stale_plan_button_names_the_expired_action() -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()

    await reject_stale_subscription_callback(callback)

    callback.answer.assert_awaited_once_with(
        "This plan button expired. Open /plan again.",
        show_alert=True,
    )
