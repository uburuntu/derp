"""Telegram presentation and interaction tests for the operator console."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import I18n

from derp.handlers.operator import (
    OperatorMaintenanceCallback,
    OperatorMaintenanceConfirmCallback,
    OperatorView,
    build_maintenance_result_panel,
    build_operator_panel,
    request_operator_maintenance,
    run_operator_maintenance,
    show_operator_console,
)
from derp.operator import (
    OperatorActivityTotals,
    OperatorArtifactTotals,
    OperatorConfirmationStore,
    OperatorConsoleSnapshot,
    OperatorControlConfig,
    OperatorDatabaseSnapshot,
    OperatorDatabaseStatus,
    OperatorMaintenanceAction,
    OperatorMaintenancePass,
    OperatorMaintenanceResult,
    OperatorNamedCount,
    OperatorPoolSnapshot,
    OperatorRuntimeSnapshot,
    OperatorStarsTotals,
    OperatorSubscriptionTotals,
    OperatorWalletTotals,
    OperatorWorkerStatus,
)


def _config() -> OperatorControlConfig:
    return OperatorControlConfig(
        environment="prod",
        service_version="0.1.0",
        public_purchases_enabled=False,
        ai_content_capture_enabled=False,
        operator_ids={42},
    )


def _counts(*names: str, nonzero: dict[str, int] | None = None):
    values = nonzero or {}
    return tuple(OperatorNamedCount(name, values.get(name, 0)) for name in names)


def _snapshot(*, degraded: bool = False) -> OperatorConsoleSnapshot:
    runtime = OperatorRuntimeSnapshot(
        uptime_seconds=90_061,
        workers=tuple(
            OperatorWorkerStatus(
                action, action is not OperatorMaintenanceAction.DELIVERIES
            )
            for action in OperatorMaintenanceAction
            if action is not OperatorMaintenanceAction.ALL
        ),
    )
    if degraded:
        database = OperatorDatabaseSnapshot.degraded()
    else:
        database = OperatorDatabaseSnapshot(
            status=OperatorDatabaseStatus.READY,
            latency_ms=4.4,
            pool=OperatorPoolSnapshot(
                size=5,
                checked_in=3,
                checked_out=1,
                overflow=0,
                open_connections=4,
            ),
            users=OperatorActivityTotals(total=1_234, recent_24h=15),
            chats=OperatorActivityTotals(total=234, recent_24h=8),
            retained_messages=OperatorActivityTotals(total=9_876, recent_24h=321),
            chats_by_type=_counts(
                "private",
                "group",
                "supergroup",
                "channel",
                nonzero={"private": 200, "supergroup": 34},
            ),
            wallet=OperatorWalletTotals(
                available_credits=1_000,
                reserved_credits=20,
                consumed_credits=5_000,
                debt_credits=3,
            ),
            operation_states=_counts(
                "quoted",
                "reserved",
                "executing",
                "captured",
                "released",
                "reversed",
                "canceled",
                "failed",
                nonzero={"captured": 20, "failed": 2},
            ),
            delivery_states=_counts(
                "not_ready",
                "pending",
                "delivering",
                "delivered",
                "uncertain",
                "failed",
                "expired",
                nonzero={"delivered": 18, "uncertain": 1},
            ),
            approval_states=_counts(
                "pending",
                "approved",
                "denied",
                "expired",
                "resumed",
                nonzero={"approved": 4},
            ),
            intent_states=_counts(
                "pending",
                "prechecked",
                "fulfilled",
                "expired",
                "canceled",
                "needs_review",
                nonzero={"fulfilled": 12},
            ),
            receipt_states=_counts(
                "received",
                "fulfilled",
                "clawed_back",
                "needs_review",
                nonzero={"fulfilled": 12},
            ),
            stars=OperatorStarsTotals(fulfilled=120, clawed_back=2),
            subscriptions=OperatorSubscriptionTotals(
                status_active=8,
                entitled=10,
                auto_renewing=7,
            ),
            artifacts=OperatorArtifactTotals(count=14, bytes=2_048),
        )
    return OperatorConsoleSnapshot(runtime=runtime, database=database)


def _callback(message: Message, *, user_id: int = 42) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = SimpleNamespace(id=user_id)
    callback.answer = AsyncMock()
    return callback


def test_every_operator_view_is_bounded_and_uses_valid_callback_data() -> None:
    for view in OperatorView:
        text, markup = build_operator_panel(view, _snapshot(), _config())

        assert 0 < len(text) <= 4_096
        assert markup.inline_keyboard
        for row in markup.inline_keyboard:
            assert 1 <= len(row) <= 8
            for button in row:
                assert button.callback_data is not None
                assert len(button.callback_data.encode()) <= 64


@pytest.mark.parametrize(
    "view",
    [OperatorView.USAGE, OperatorView.COMMERCE, OperatorView.OPERATIONS],
)
def test_database_views_degrade_without_partial_or_private_data(
    view: OperatorView,
) -> None:
    text, _ = build_operator_panel(view, _snapshot(degraded=True), _config())

    assert "Database diagnostics are unavailable" in text
    assert "message" not in text.lower()


def test_overview_surfaces_aggregate_attention_without_identifiers() -> None:
    text, _ = build_operator_panel(OperatorView.OVERVIEW, _snapshot(), _config())

    assert "prod · up 1d 1h" in text
    assert "Attention: 5 signals" in text
    assert "12345" not in text


def test_russian_operator_views_are_concise_and_use_derp_persona(
    setup_i18n: I18n,
) -> None:
    with setup_i18n.use_locale("ru"):
        rendered = {
            view: build_operator_panel(view, _snapshot(), _config())[0]
            for view in OperatorView
        }

    assert "Дерп 0.1.0" in rendered[OperatorView.RUNTIME]
    assert "1\xa0234" in rendered[OperatorView.USAGE]
    assert all(
        "Operator" not in text and len(text) <= 4_096 for text in rendered.values()
    )


def test_maintenance_result_is_conservative_and_compact() -> None:
    result = OperatorMaintenanceResult(
        requested_action=OperatorMaintenanceAction.OPERATIONS,
        passes=(
            OperatorMaintenancePass(
                action=OperatorMaintenanceAction.OPERATIONS,
                counts=(
                    OperatorNamedCount("examined_count", 3),
                    OperatorNamedCount("expired_quote_count", 0),
                    OperatorNamedCount("incomplete_result_count", 1),
                ),
            ),
        ),
        duration_ms=12.6,
    )

    text, markup = build_maintenance_result_panel(result)

    assert "Maintenance pass completed" in text
    assert "Examined operations: 3" in text
    assert "Incomplete results: 1" in text
    assert "Expired quotes" not in text
    assert "succeeded" not in text.lower()
    assert markup.inline_keyboard


async def test_private_operator_command_opens_protected_overview(make_message) -> None:
    message = make_message(text="/operator", chat_id=42, chat_type="private")
    message.from_user.id = 42
    console = MagicMock()
    console.snapshot = AsyncMock(return_value=_snapshot())

    await show_operator_console(message, console, _config())

    console.snapshot.assert_awaited_once()
    assert "Operator console" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs["protect_content"] is True


async def test_group_operator_command_redirects_without_loading_stats(
    make_message,
) -> None:
    message = make_message(text="/operator", chat_id=-100, chat_type="supergroup")
    message.from_user.id = 42
    console = MagicMock()
    console.snapshot = AsyncMock()

    await show_operator_console(message, console, _config())

    console.snapshot.assert_not_awaited()
    assert "/operator" in message.reply.await_args.args[0]


async def test_maintenance_confirmation_is_bound_then_consumed_once(
    make_message,
) -> None:
    message = make_message(text="operator", chat_id=42, chat_type="private")
    message.edit_text = AsyncMock()
    callback = _callback(message)
    store = OperatorConfirmationStore(token_factory=lambda: "fixed_operator_token")
    action = OperatorMaintenanceAction.HISTORY

    await request_operator_maintenance(
        callback,
        OperatorMaintenanceCallback(action=action),
        store,
    )

    callback.answer.assert_awaited_once_with()
    markup = message.edit_text.await_args.kwargs["reply_markup"]
    packed = markup.inline_keyboard[0][0].callback_data
    confirm = OperatorMaintenanceConfirmCallback.unpack(packed)
    assert confirm.action is action
    assert confirm.token == "fixed_operator_token"

    result = OperatorMaintenanceResult(
        requested_action=action,
        passes=(
            OperatorMaintenancePass(
                action=action,
                counts=(OperatorNamedCount("purged_message_count", 2),),
            ),
        ),
        duration_ms=5,
    )
    console = MagicMock()
    console.run_maintenance = AsyncMock(return_value=result)
    message.edit_text.reset_mock()
    callback.answer.reset_mock()

    await run_operator_maintenance(callback, confirm, console, store)

    callback.answer.assert_awaited_once_with("Maintenance started")
    console.run_maintenance.assert_awaited_once_with(action, actor_id=42)
    assert "Purged messages: 2" in message.edit_text.await_args.args[0]

    callback.answer.reset_mock()
    await run_operator_maintenance(callback, confirm, console, store)
    callback.answer.assert_awaited_once_with(
        "This confirmation expired. Choose the action again.",
        show_alert=True,
    )
    assert console.run_maintenance.await_count == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"environment": "<prod>"},
        {"service_version": ""},
        {"public_purchases_enabled": 1},
        {"ai_content_capture_enabled": 0},
        {"operator_ids": {0}},
    ],
)
def test_operator_control_config_rejects_unsafe_values(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "environment": "prod",
        "service_version": "0.1.0",
        "public_purchases_enabled": False,
        "ai_content_capture_enabled": False,
        "operator_ids": {42},
    }
    values.update(kwargs)

    with pytest.raises((TypeError, ValueError)):
        OperatorControlConfig(**values)  # type: ignore[arg-type]
