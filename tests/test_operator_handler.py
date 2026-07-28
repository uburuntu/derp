"""Telegram presentation and interaction tests for the operator console."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import I18n

from derp.handlers.operator import (
    OperatorDebugRefundConfirmCallback,
    OperatorMaintenanceCallback,
    OperatorMaintenanceConfirmCallback,
    OperatorSupportAction,
    OperatorSupportResolveCallback,
    OperatorSupportResolveConfirmCallback,
    OperatorView,
    build_maintenance_result_panel,
    build_operator_panel,
    build_operator_support_queue,
    check_operator_inference,
    request_operator_maintenance,
    request_operator_support_resolution,
    request_operator_test_refund,
    resolve_operator_support_case,
    run_operator_maintenance,
    run_operator_test_refund,
    show_operator_console,
    show_operator_support_queue,
)
from derp.history.capture import capture_outbound_history, should_capture_outbound
from derp.operator import (
    MAX_CONFIRMATION_TOKEN_LENGTH,
    OperatorActivityTotals,
    OperatorArtifactTotals,
    OperatorConfirmationStore,
    OperatorConsoleSnapshot,
    OperatorControlConfig,
    OperatorDatabaseSnapshot,
    OperatorDatabaseStatus,
    OperatorDebugRefundResult,
    OperatorInferenceAttemptTotals,
    OperatorInferenceCatalogSnapshot,
    OperatorInferenceConnectivitySnapshot,
    OperatorInferenceSnapshot,
    OperatorInferenceTokenTotals,
    OperatorInferenceUsageTotals,
    OperatorMaintenanceAction,
    OperatorMaintenancePass,
    OperatorMaintenanceResult,
    OperatorNamedCount,
    OperatorPaymentUpdateTotals,
    OperatorPoolSnapshot,
    OperatorProbeStatus,
    OperatorRuntimeSnapshot,
    OperatorStarsTotals,
    OperatorSubscriptionTotals,
    OperatorSupportTotals,
    OperatorWalletTotals,
    OperatorWorkerStatus,
)
from derp.support import (
    OperatorSupportCase,
    ResolveSupportResult,
    SupportKind,
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
            support=OperatorSupportTotals(open=2, payment_open=1, resolved=4),
            payment_updates=OperatorPaymentUpdateTotals(
                pending=0,
                processing=0,
                completed=12,
                attention=0,
                due=0,
                reply_failed=0,
                reply_skipped=0,
            ),
            refund_request_states=_counts(
                "pending",
                "submitting",
                "accepted",
                "rejected",
                "needs_review",
                "reconciled",
                nonzero={"reconciled": 1},
            ),
            subscriptions=OperatorSubscriptionTotals(
                status_active=8,
                entitled=10,
                auto_renewing=7,
                renewal_pending=2,
                renewal_processing=1,
                renewal_attention=1,
                renewal_due=1,
            ),
            artifacts=OperatorArtifactTotals(count=14, bytes=2_048),
        )
    inference = OperatorInferenceSnapshot(
        catalog=OperatorInferenceCatalogSnapshot(
            verified_on=date(2026, 7, 21),
            enabled_roles=("chat_standard", "image", "tts"),
            available_roles=("chat_standard", "image"),
        ),
        connectivity=OperatorInferenceConnectivitySnapshot(
            balance_status=OperatorProbeStatus.READY,
            catalog_status=OperatorProbeStatus.READY,
            key_limit_usd=Decimal("10"),
            key_usage_usd=Decimal("2.25"),
            key_remaining_usd=Decimal("7.75"),
            catalog_model_count=321,
            visible_enabled_roles=("chat_standard", "image"),
        ),
        usage=None
        if degraded
        else OperatorInferenceUsageTotals(
            attempts=OperatorInferenceAttemptTotals(
                total=50,
                recent_24h=10,
                succeeded=42,
                succeeded_24h=8,
                failed=5,
                failed_24h=1,
                pending=3,
                pending_24h=1,
            ),
            tokens=OperatorInferenceTokenTotals(
                input=10_000,
                output=2_000,
                total=12_000,
                cache_read=4_000,
                cache_write=500,
                reasoning=800,
                audio_input=100,
                audio_output=200,
            ),
            pending_cost_reconciliation=2,
            unavailable_cost_count=1,
            route_policy_violation_count=0,
            reconciled_cost_usd=Decimal("1.234567"),
        ),
    )
    return OperatorConsoleSnapshot(
        runtime=runtime,
        database=database,
        inference=inference,
    )


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


def test_support_queue_is_bounded_and_exposes_only_required_case_fields() -> None:
    case = OperatorSupportCase(
        reference="CASE123456",
        kind=SupportKind.PRIVACY,
        created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        requester_telegram_id=123_456,
    )

    text, markup = build_operator_support_queue((case,))

    assert "CASE123456" in text
    assert "Privacy or data" in text
    assert "2026-07-28 12:00 UTC" in text
    assert "123456" in text
    resolve = OperatorSupportResolveCallback.unpack(
        markup.inline_keyboard[0][0].callback_data
    )
    assert resolve.reference == case.reference
    assert all(
        len(button.callback_data.encode()) <= 64
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    )


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
    assert "Attention: 9 signals" in text
    assert "12345" not in text


def test_inference_view_shows_accounting_and_read_only_connectivity() -> None:
    text, markup = build_operator_panel(
        OperatorView.INFERENCE,
        _snapshot(),
        _config(),
    )

    assert "Attempts: 10 / 24h · 50 total" in text
    assert "success 8/42 · failed 1/5 · pending 1/3" in text
    assert "Cost: $1.234567 reconciled · 2 pending · 1 unavailable" in text
    assert "Input 10,000 · output 2,000 · total 12,000" in text
    assert "Roles: 2/3 available · reviewed 2026-07-21" in text
    assert "Available roles: <code>chat_standard</code> · <code>image</code>" in text
    assert "Unavailable roles: <code>tts</code>" in text
    assert "OpenRouter key: $7.75 of $10 left · $2.25 used" in text
    assert "OpenRouter catalog: 321 models · 2/3 enabled visible" in text
    assert "reviewed-model" not in text
    assert markup.inline_keyboard[0][0].text == "Run read-only check"


def test_commerce_view_shows_conservative_project_economics() -> None:
    text, _ = build_operator_panel(OperatorView.COMMERCE, _snapshot(), _config())

    assert (
        "Economics floor: $1.2 revenue · $1.234567 reconciled AI cost · "
        "margin incomplete (2 pending, 1 unavailable)"
    ) in text
    assert "Support: 2 open · 1 payment · 4 resolved" in text
    assert "Renewal queue: 2 pending · 1 processing · 1 attention · 1 due" in text


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


def test_payment_maintenance_has_a_control_and_clear_recovery_counts() -> None:
    _, markup = build_operator_panel(
        OperatorView.MAINTENANCE,
        _snapshot(),
        _config(),
    )
    actions = {
        OperatorMaintenanceCallback.unpack(button.callback_data).action
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("opm:")
    }
    result = OperatorMaintenanceResult(
        requested_action=OperatorMaintenanceAction.PAYMENTS,
        passes=(
            OperatorMaintenancePass(
                action=OperatorMaintenanceAction.PAYMENTS,
                counts=(
                    OperatorNamedCount("payment_update_settled_count", 2),
                    OperatorNamedCount("debug_refund_reconciled_count", 1),
                    OperatorNamedCount("debug_refund_pending_review_count", 1),
                ),
            ),
        ),
        duration_ms=8.0,
    )

    text, _ = build_maintenance_result_panel(result)

    assert OperatorMaintenanceAction.PAYMENTS in actions
    assert "Payment updates settled: 2" in text
    assert "Test refunds reconciled: 1" in text
    assert "Test refunds needing review: 1" in text


def test_longest_confirmation_capability_fits_telegram_callback_limit() -> None:
    store = OperatorConfirmationStore(
        token_factory=lambda: "x" * MAX_CONFIRMATION_TOKEN_LENGTH
    )
    action = OperatorMaintenanceAction.SUBSCRIPTIONS
    token = store.issue(actor_id=42, action=action)

    packed = OperatorMaintenanceConfirmCallback(action=action, token=token).pack()

    assert len(packed.encode("ascii")) <= 64


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


async def test_inference_check_runs_metadata_reads_then_refreshes_page(
    make_message,
) -> None:
    message = make_message(text="operator", chat_id=42, chat_type="private")
    message.edit_text = AsyncMock()
    callback = _callback(message)
    console = MagicMock()
    console.check_inference_connectivity = AsyncMock()
    console.snapshot = AsyncMock(return_value=_snapshot())

    await check_operator_inference(callback, console, _config())

    callback.answer.assert_awaited_once_with("Checking inference")
    console.check_inference_connectivity.assert_awaited_once_with(42)
    console.snapshot.assert_awaited_once_with()
    assert message.edit_text.await_count == 1
    assert message.edit_text.await_args.args[0].startswith("<b>Inference</b>")


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


async def test_debug_refund_is_fixed_actor_bound_and_single_use(make_message) -> None:
    message = make_message(text="operator", chat_id=42, chat_type="private")
    message.edit_text = AsyncMock()
    callback = _callback(message)
    store = OperatorConfirmationStore(token_factory=lambda: "fixed_refund_token")

    await request_operator_test_refund(callback, store)

    markup = message.edit_text.await_args.kwargs["reply_markup"]
    packed = markup.inline_keyboard[0][0].callback_data
    confirm = OperatorDebugRefundConfirmCallback.unpack(packed)
    assert confirm.token == "fixed_refund_token"

    refunds = MagicMock()
    refunds.refund_latest = AsyncMock(return_value=OperatorDebugRefundResult.REQUESTED)
    message.edit_text.reset_mock()
    callback.answer.reset_mock()

    await run_operator_test_refund(callback, confirm, store, refunds)

    callback.answer.assert_awaited_once_with("Requesting refund")
    refunds.refund_latest.assert_awaited_once_with(42)
    assert "test credits were reconciled" in message.edit_text.await_args.args[0]

    callback.answer.reset_mock()
    await run_operator_test_refund(callback, confirm, store, refunds)
    callback.answer.assert_awaited_once_with(
        "This confirmation expired. Choose the action again.",
        show_alert=True,
    )
    assert refunds.refund_latest.await_count == 1


async def test_support_queue_refresh_reads_durable_service(make_message) -> None:
    message = make_message(text="operator", chat_id=42, chat_type="private")
    capture_states: list[bool] = []

    async def render(*args, **kwargs) -> None:
        capture_states.append(should_capture_outbound())

    message.edit_text = AsyncMock(side_effect=render)
    callback = _callback(message)
    case = OperatorSupportCase(
        reference="CASE123456",
        kind=SupportKind.PAYMENT,
        created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        requester_telegram_id=77,
    )
    support = MagicMock()
    support.list_operator_open = AsyncMock(return_value=(case,))

    with capture_outbound_history():
        await show_operator_support_queue(callback, support)
        assert should_capture_outbound()

    support.list_operator_open.assert_awaited_once_with()
    assert "CASE123456" in message.edit_text.await_args.args[0]
    assert "user <code>77</code>" in message.edit_text.await_args.args[0]
    assert capture_states == [False]


async def test_support_resolution_is_case_bound_single_use_and_notifies_requester(
    make_message,
) -> None:
    message = make_message(text="operator", chat_id=42, chat_type="private")
    message.edit_text = AsyncMock()
    callback = _callback(message)
    case = OperatorSupportCase(
        reference="CASE123456",
        kind=SupportKind.REFUND,
        created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        requester_telegram_id=77,
    )
    support = MagicMock()
    support.get_operator_open = AsyncMock(return_value=case)
    effects: list[str] = []

    async def resolve(reference: str) -> ResolveSupportResult:
        effects.append(f"resolve:{reference}")
        return ResolveSupportResult(
            case=case,
            resolved_at=datetime(2026, 7, 28, 12, 5, tzinfo=UTC),
            changed=True,
        )

    async def notify(*args, **kwargs) -> None:
        effects.append("notify")

    support.resolve_operator = AsyncMock(side_effect=resolve)
    support.list_operator_open = AsyncMock(return_value=())
    store = OperatorConfirmationStore(token_factory=lambda: "fixed_support_token")

    await request_operator_support_resolution(
        callback,
        OperatorSupportResolveCallback(reference=case.reference),
        support,
        store,
    )

    markup = message.edit_text.await_args.kwargs["reply_markup"]
    packed = markup.inline_keyboard[0][0].callback_data
    assert case.reference not in packed
    confirm = OperatorSupportResolveConfirmCallback.unpack(packed)
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=notify)
    message.edit_text.reset_mock()
    callback.answer.reset_mock()

    await resolve_operator_support_case(
        callback,
        confirm,
        support,
        store,
        bot,
    )

    callback.answer.assert_awaited_once_with("Resolving case")
    support.resolve_operator.assert_awaited_once_with(case.reference)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 77
    assert case.reference in bot.send_message.await_args.args[1]
    assert effects == ["notify", f"resolve:{case.reference}"]
    assert "requester was notified" in message.edit_text.await_args.args[0]
    assert "No open cases" in message.edit_text.await_args.args[0]

    callback.answer.reset_mock()
    await resolve_operator_support_case(
        callback,
        confirm,
        support,
        store,
        bot,
    )
    callback.answer.assert_awaited_once_with(
        "This confirmation expired. Choose the action again.",
        show_alert=True,
    )
    assert support.resolve_operator.await_count == 1


async def test_support_notification_failure_keeps_case_open(make_message) -> None:
    message = make_message(text="operator", chat_id=42, chat_type="private")
    message.edit_text = AsyncMock()
    callback = _callback(message)
    case = OperatorSupportCase(
        reference="CASE654321",
        kind=SupportKind.ACCESS,
        created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        requester_telegram_id=88,
    )
    support = MagicMock()
    support.get_operator_open = AsyncMock(return_value=case)
    support.resolve_operator = AsyncMock()
    support.list_operator_open = AsyncMock(return_value=(case,))
    store = OperatorConfirmationStore(token_factory=lambda: "failed_notice_token")
    token = store.issue(
        actor_id=42,
        action=OperatorSupportAction.RESOLVE,
        resource_key=case.reference,
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))

    await resolve_operator_support_case(
        callback,
        OperatorSupportResolveConfirmCallback(token=token),
        support,
        store,
        bot,
    )

    support.resolve_operator.assert_not_awaited()
    support.list_operator_open.assert_awaited_once_with()
    assert "remains open" in message.edit_text.await_args.args[0]
    assert case.reference in message.edit_text.await_args.args[0]


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
