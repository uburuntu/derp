"""Private Telegram control surface for trusted bot operators."""

from __future__ import annotations

import sys
from datetime import UTC
from decimal import Decimal
from enum import StrEnum
from html import escape
from importlib.metadata import PackageNotFoundError, version

import logfire
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _

from derp.billing import DEFAULT_STAR_ECONOMICS
from derp.catalog import CATALOG_VERIFIED_ON, GOOGLE_MODEL_CATALOG
from derp.command_menu import configure_bot_command_menu
from derp.common.localization import format_local_integer
from derp.common.sender import MessageSender
from derp.handlers.debug import debug_buy_command
from derp.history.capture import suppress_outbound_history
from derp.observability import report_exception
from derp.operator import (
    OperatorConfirmationCapacityError,
    OperatorConfirmationStore,
    OperatorConsoleService,
    OperatorConsoleSnapshot,
    OperatorControlConfig,
    OperatorDatabaseSnapshot,
    OperatorDebugRefundResult,
    OperatorDebugRefundService,
    OperatorInferenceConnectivitySnapshot,
    OperatorMaintenanceAction,
    OperatorMaintenanceResult,
    OperatorNamedCount,
    OperatorOnlyFilter,
    OperatorProbeStatus,
)
from derp.support import OperatorSupportCase, SupportKind, SupportRequestService

router = Router(name="operator")
purchase_test_router = Router(name="operator_purchase_test")
stale_callback_router = Router(name="operator_stale_callback")
rejection_router = Router(name="operator_rejection")
router.message.filter(OperatorOnlyFilter())
router.callback_query.filter(OperatorOnlyFilter())
_OPERATOR_CALLBACK_PREFIXES = (
    "op:",
    "opm:",
    "opx:",
    "opr:",
    "opu:",
    "opq:",
    "opqr:",
    "opqc:",
)


class OperatorView(StrEnum):
    """Stable pages in the edit-in-place operator console."""

    OVERVIEW = "home"
    USAGE = "usage"
    COMMERCE = "money"
    OPERATIONS = "ops"
    INFERENCE = "inference"
    RUNTIME = "runtime"
    MAINTENANCE = "maint"
    TESTS = "tests"


class OperatorUtility(StrEnum):
    """Non-maintenance controls exposed by the console."""

    TEST_PURCHASE = "buy"
    TEST_REFUND = "refund"
    SYNC_COMMANDS = "commands"
    INFERENCE_CHECK = "inference"


class OperatorSupportQueueAction(StrEnum):
    """Fixed queue actions that never carry requester data."""

    LIST = "list"


class OperatorSupportAction(StrEnum):
    """Destructive support actions requiring an actor-bound capability."""

    RESOLVE = "resolve"


class OperatorNavigationCallback(CallbackData, prefix="op"):
    view: OperatorView


class OperatorMaintenanceCallback(CallbackData, prefix="opm"):
    action: OperatorMaintenanceAction


class OperatorMaintenanceConfirmCallback(CallbackData, prefix="opx"):
    action: OperatorMaintenanceAction
    token: str


class OperatorDebugRefundConfirmCallback(CallbackData, prefix="opr"):
    token: str


class OperatorUtilityCallback(CallbackData, prefix="opu"):
    action: OperatorUtility


class OperatorSupportQueueCallback(CallbackData, prefix="opq"):
    action: OperatorSupportQueueAction


class OperatorSupportResolveCallback(CallbackData, prefix="opqr"):
    reference: str


class OperatorSupportResolveConfirmCallback(CallbackData, prefix="opqc"):
    token: str


def build_operator_panel(
    view: OperatorView,
    snapshot: OperatorConsoleSnapshot,
    config: OperatorControlConfig,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render one bounded operator view from identifier-free state."""
    if view is OperatorView.OVERVIEW:
        return _overview_panel(snapshot, config)
    if view is OperatorView.USAGE:
        return _usage_panel(snapshot)
    if view is OperatorView.COMMERCE:
        return _commerce_panel(snapshot)
    if view is OperatorView.OPERATIONS:
        return _operations_panel(snapshot)
    if view is OperatorView.INFERENCE:
        return _inference_panel(snapshot)
    if view is OperatorView.RUNTIME:
        return _runtime_panel(snapshot, config)
    if view is OperatorView.MAINTENANCE:
        return _maintenance_panel()
    if view is OperatorView.TESTS:
        return _tests_panel(snapshot)
    raise ValueError("unsupported operator view")


def build_maintenance_result_panel(
    result: OperatorMaintenanceResult,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render conservative worker observations without claiming success."""
    sections = [_("<b>Maintenance pass completed</b>")]
    for maintenance_pass in result.passes:
        counts = [
            _("{label}: {count}").format(
                label=_maintenance_count_label(item.name),
                count=_number(item.count),
            )
            for item in maintenance_pass.counts
            if item.count
        ]
        sections.append(
            "\n".join(
                [
                    f"<b>{escape(_maintenance_label(maintenance_pass.action))}</b>",
                    *(counts or [_("No changes reported")]),
                ]
            )
        )
    sections.append(
        _("Duration: {duration} ms").format(
            duration=_number(round(result.duration_ms)),
        )
    )
    return "\n\n".join(sections), _section_navigation(OperatorView.MAINTENANCE)


def build_operator_support_queue(
    cases: tuple[OperatorSupportCase, ...],
    *,
    notice: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the bounded durable queue with operator-only actor identifiers."""
    sections = [_("<b>Support queue</b>")]
    if notice:
        sections.append(notice)
    if cases:
        sections.extend(
            _(
                "<code>{reference}</code> · {kind}\n"
                "{created_at} · user <code>{requester_id}</code>"
            ).format(
                reference=escape(case.reference),
                kind=escape(_support_kind(case.kind)),
                created_at=escape(_support_created_at(case)),
                requester_id=case.requester_telegram_id,
            )
            for case in cases
        )
    else:
        sections.append(_("No open cases."))
    rows = [
        [
            InlineKeyboardButton(
                text=_("Resolve {reference}").format(reference=case.reference),
                callback_data=OperatorSupportResolveCallback(
                    reference=case.reference
                ).pack(),
            )
        ]
        for case in cases
    ]
    rows.append(
        [
            _nav_button(_("Back"), OperatorView.COMMERCE),
            InlineKeyboardButton(
                text=_("Refresh"),
                callback_data=OperatorSupportQueueCallback(
                    action=OperatorSupportQueueAction.LIST
                ).pack(),
            ),
        ]
    )
    return "\n\n".join(sections), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("operator", "ops"))
async def show_operator_console(
    message: Message,
    operator_console: OperatorConsoleService,
    operator_config: OperatorControlConfig,
) -> None:
    """Open the control surface only in the operator's private chat."""
    if (
        message.chat.type != "private"
        or message.from_user is None
        or message.chat.id != message.from_user.id
    ):
        await message.reply(
            _("Operator console is private. Open Derp directly and use /operator.")
        )
        return
    try:
        snapshot = await operator_console.snapshot()
        text, markup = build_operator_panel(
            OperatorView.OVERVIEW,
            snapshot,
            operator_config,
        )
        with suppress_outbound_history():
            await message.answer(text, reply_markup=markup, protect_content=True)
    except Exception as exc:
        report_exception(
            "operator.panel_open_failed",
            exception=exc,
            operator_id=message.from_user.id,
        )
        await message.answer(
            _("Operator console is unavailable. Try again."),
            protect_content=True,
        )


@router.callback_query(OperatorNavigationCallback.filter())
async def navigate_operator_console(
    callback: CallbackQuery,
    callback_data: OperatorNavigationCallback,
    operator_console: OperatorConsoleService,
    operator_config: OperatorControlConfig,
) -> None:
    """Acknowledge immediately, then replace the current panel view."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    await callback.answer()
    await _refresh_panel(
        message,
        view=callback_data.view,
        operator_console=operator_console,
        operator_config=operator_config,
        operator_id=callback.from_user.id,
    )


@router.callback_query(OperatorMaintenanceCallback.filter())
async def request_operator_maintenance(
    callback: CallbackQuery,
    callback_data: OperatorMaintenanceCallback,
    operator_confirmations: OperatorConfirmationStore,
) -> None:
    """Issue a short-lived capability before offering a maintenance action."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    try:
        token = operator_confirmations.issue(
            actor_id=callback.from_user.id,
            action=callback_data.action,
        )
    except OperatorConfirmationCapacityError:
        await callback.answer(
            _("Too many pending confirmations. Try again shortly."),
            show_alert=True,
        )
        return
    await callback.answer()
    text = _(
        "<b>Confirm maintenance</b>\n{action}\n\nThis runs the live worker now."
    ).format(action=escape(_maintenance_label(callback_data.action)))
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Run now"),
                    callback_data=OperatorMaintenanceConfirmCallback(
                        action=callback_data.action,
                        token=token,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Cancel"),
                    callback_data=OperatorNavigationCallback(
                        view=OperatorView.MAINTENANCE
                    ).pack(),
                )
            ],
        ]
    )
    await _edit_operator_message(
        message,
        text,
        markup,
        operator_id=callback.from_user.id,
        event="operator.maintenance_confirmation_render_failed",
    )


@router.callback_query(OperatorMaintenanceConfirmCallback.filter())
async def run_operator_maintenance(
    callback: CallbackQuery,
    callback_data: OperatorMaintenanceConfirmCallback,
    operator_console: OperatorConsoleService,
    operator_confirmations: OperatorConfirmationStore,
) -> None:
    """Consume an exact confirmation and run the serialized live worker pass."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    if not operator_confirmations.consume(
        callback_data.token,
        actor_id=callback.from_user.id,
        action=callback_data.action,
    ):
        await callback.answer(
            _("This confirmation expired. Choose the action again."),
            show_alert=True,
        )
        return
    await callback.answer(_("Maintenance started"))
    try:
        result = await operator_console.run_maintenance(
            callback_data.action,
            actor_id=callback.from_user.id,
        )
        text, markup = build_maintenance_result_panel(result)
    except Exception as exc:
        report_exception(
            "operator.maintenance_pass_failed",
            exception=exc,
            operator_id=callback.from_user.id,
            action=callback_data.action.value,
        )
        text = _(
            "<b>Maintenance interrupted</b>\n"
            "The pass did not complete. Check telemetry before retrying."
        )
        markup = _section_navigation(OperatorView.MAINTENANCE)
    await _edit_operator_message(
        message,
        text,
        markup,
        operator_id=callback.from_user.id,
        event="operator.maintenance_result_render_failed",
    )


@purchase_test_router.callback_query(
    OperatorUtilityCallback.filter(F.action == OperatorUtility.TEST_PURCHASE)
)
async def open_operator_test_purchase(
    callback: CallbackQuery,
    sender: MessageSender,
) -> None:
    """Open the existing production-shaped one-Star validation path."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    await callback.answer(_("Opening test checkout"))
    try:
        sender.protect_content = True
        await debug_buy_command(message, sender)
    except Exception as exc:
        report_exception(
            "operator.test_purchase_open_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        await message.answer(
            _("Test checkout is unavailable. Try again."),
            protect_content=True,
        )


@router.callback_query(
    OperatorUtilityCallback.filter(F.action == OperatorUtility.TEST_REFUND)
)
async def request_operator_test_refund(
    callback: CallbackQuery,
    operator_confirmations: OperatorConfirmationStore,
) -> None:
    """Confirm the fixed latest-debug-purchase refund capability."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    try:
        token = operator_confirmations.issue(
            actor_id=callback.from_user.id,
            action=OperatorUtility.TEST_REFUND,
        )
    except OperatorConfirmationCapacityError:
        await callback.answer(
            _("Too many pending confirmations. Try again shortly."),
            show_alert=True,
        )
        return
    await callback.answer()
    await _edit_operator_message(
        message,
        _(
            "<b>Refund latest test?</b>\n"
            "Only your latest fulfilled 1-Star operator test can be refunded."
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Refund test"),
                        callback_data=OperatorDebugRefundConfirmCallback(
                            token=token
                        ).pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=_("Cancel"),
                        callback_data=OperatorNavigationCallback(
                            view=OperatorView.TESTS
                        ).pack(),
                    )
                ],
            ]
        ),
        operator_id=callback.from_user.id,
        event="operator.test_refund_confirmation_render_failed",
    )


@router.callback_query(OperatorDebugRefundConfirmCallback.filter())
async def run_operator_test_refund(
    callback: CallbackQuery,
    callback_data: OperatorDebugRefundConfirmCallback,
    operator_confirmations: OperatorConfirmationStore,
    operator_debug_refunds: OperatorDebugRefundService,
) -> None:
    """Consume one confirmation and request the exact server-selected refund."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    if not operator_confirmations.consume(
        callback_data.token,
        actor_id=callback.from_user.id,
        action=OperatorUtility.TEST_REFUND,
    ):
        await callback.answer(
            _("This confirmation expired. Choose the action again."),
            show_alert=True,
        )
        return
    await callback.answer(_("Requesting refund"))
    try:
        result = await operator_debug_refunds.refund_latest(callback.from_user.id)
    except Exception as exc:
        report_exception(
            "operator.test_refund_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        text = _(
            "<b>Test refund failed</b>\n"
            "Telegram did not confirm it. Check telemetry before retrying."
        )
    else:
        if result is OperatorDebugRefundResult.REQUESTED:
            logfire.info(
                "operator.test_refund_requested",
                operator_id=callback.from_user.id,
            )
            text = _(
                "<b>Test refund requested</b>\n"
                "Telegram accepted it and the test credits were reconciled."
            )
        elif result is OperatorDebugRefundResult.PENDING:
            text = _(
                "<b>Test refund needs review</b>\n"
                "The provider outcome is pending. Don't retry it; check Commerce."
            )
        elif result is OperatorDebugRefundResult.REJECTED:
            text = _(
                "<b>Test refund rejected</b>\n"
                "Telegram confirmed that no refund was sent. The test can be retried."
            )
        else:
            text = _(
                "<b>No refundable test</b>\n"
                "Complete the 1-Star checkout first, or wait for its settlement."
            )
    await _edit_operator_message(
        message,
        text,
        _section_navigation(OperatorView.TESTS),
        operator_id=callback.from_user.id,
        event="operator.test_refund_result_render_failed",
    )


@router.callback_query(
    OperatorUtilityCallback.filter(F.action == OperatorUtility.SYNC_COMMANDS)
)
async def sync_operator_command_menu(
    callback: CallbackQuery,
    i18n: I18n,
    operator_config: OperatorControlConfig,
) -> None:
    """Reconcile every localized Telegram command scope on demand."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    await callback.answer(_("Command sync started"))
    try:
        await configure_bot_command_menu(
            callback.bot,
            i18n=i18n,
            public_purchases_enabled=operator_config.public_purchases_enabled,
            operator_ids=operator_config.operator_ids,
        )
    except Exception as exc:
        report_exception(
            "operator.command_menu_sync_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        text = _(
            "<b>Command sync failed</b>\n"
            "Telegram did not accept the complete command state."
        )
    else:
        logfire.info(
            "operator.command_menu_sync_completed",
            operator_id=callback.from_user.id,
        )
        text = _(
            "<b>Command menu synced</b>\n"
            "Current locales and configured scopes now match."
        )
    await _edit_operator_message(
        message,
        text,
        _section_navigation(OperatorView.TESTS),
        operator_id=callback.from_user.id,
        event="operator.command_menu_result_render_failed",
    )


@router.callback_query(
    OperatorUtilityCallback.filter(F.action == OperatorUtility.INFERENCE_CHECK)
)
async def check_operator_inference(
    callback: CallbackQuery,
    operator_console: OperatorConsoleService,
    operator_config: OperatorControlConfig,
) -> None:
    """Run only provider metadata reads, then refresh the inference page."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    await callback.answer(_("Checking inference"))
    try:
        await operator_console.check_inference_connectivity(callback.from_user.id)
    except Exception as exc:
        report_exception(
            "operator.inference_read_check_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        await _edit_operator_message(
            message,
            _(
                "<b>Inference check failed</b>\n"
                "No inference request was sent. Check telemetry."
            ),
            _inference_navigation(),
            operator_id=callback.from_user.id,
            event="operator.inference_check_result_render_failed",
        )
        return
    await _refresh_panel(
        message,
        view=OperatorView.INFERENCE,
        operator_console=operator_console,
        operator_config=operator_config,
        operator_id=callback.from_user.id,
    )


@router.callback_query(OperatorSupportQueueCallback.filter())
async def show_operator_support_queue(
    callback: CallbackQuery,
    support_requests: SupportRequestService,
) -> None:
    """Refresh the support queue directly from its durable database state."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    await callback.answer()
    await _refresh_support_queue(
        message,
        support_requests=support_requests,
        operator_id=callback.from_user.id,
    )


@router.callback_query(OperatorSupportResolveCallback.filter())
async def request_operator_support_resolution(
    callback: CallbackQuery,
    callback_data: OperatorSupportResolveCallback,
    support_requests: SupportRequestService,
    operator_confirmations: OperatorConfirmationStore,
) -> None:
    """Bind one exact open case to a short-lived resolution capability."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    try:
        case = await support_requests.get_operator_open(callback_data.reference)
    except Exception as exc:
        report_exception(
            "operator.support_case_read_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        await callback.answer(_("Support queue is unavailable."), show_alert=True)
        return
    if case is None:
        await callback.answer(_("This case is no longer open."), show_alert=True)
        await _refresh_support_queue(
            message,
            support_requests=support_requests,
            operator_id=callback.from_user.id,
        )
        return
    try:
        token = operator_confirmations.issue(
            actor_id=callback.from_user.id,
            action=OperatorSupportAction.RESOLVE,
            resource_key=case.reference,
        )
    except OperatorConfirmationCapacityError:
        await callback.answer(
            _("Too many pending confirmations. Try again shortly."),
            show_alert=True,
        )
        return
    await callback.answer()
    text = _(
        "<b>Resolve support case?</b>\n"
        "<code>{reference}</code> · {kind}\n"
        "{created_at} · user <code>{requester_id}</code>"
    ).format(
        reference=escape(case.reference),
        kind=escape(_support_kind(case.kind)),
        created_at=escape(_support_created_at(case)),
        requester_id=case.requester_telegram_id,
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Resolve case"),
                    callback_data=OperatorSupportResolveConfirmCallback(
                        token=token
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Cancel"),
                    callback_data=OperatorSupportQueueCallback(
                        action=OperatorSupportQueueAction.LIST
                    ).pack(),
                )
            ],
        ]
    )
    await _edit_operator_message(
        message,
        text,
        markup,
        operator_id=callback.from_user.id,
        event="operator.support_confirmation_render_failed",
    )


@router.callback_query(OperatorSupportResolveConfirmCallback.filter())
async def resolve_operator_support_case(
    callback: CallbackQuery,
    callback_data: OperatorSupportResolveConfirmCallback,
    support_requests: SupportRequestService,
    operator_confirmations: OperatorConfirmationStore,
    bot: Bot,
) -> None:
    """Notify the requester, then resolve the server-bound case once."""
    message = await _private_operator_callback(callback)
    if message is None:
        return
    reference = operator_confirmations.consume_resource(
        callback_data.token,
        actor_id=callback.from_user.id,
        action=OperatorSupportAction.RESOLVE,
    )
    if reference is None:
        await callback.answer(
            _("This confirmation expired. Choose the action again."),
            show_alert=True,
        )
        return
    await callback.answer(_("Resolving case"))
    try:
        case = await support_requests.get_operator_open(reference)
    except Exception as exc:
        report_exception(
            "operator.support_case_read_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        await _refresh_support_queue(
            message,
            support_requests=support_requests,
            operator_id=callback.from_user.id,
            notice=_("Support queue is unavailable. The case was not changed."),
        )
        return
    if case is None:
        await _refresh_support_queue(
            message,
            support_requests=support_requests,
            operator_id=callback.from_user.id,
            notice=_("The case was already resolved."),
        )
        return

    try:
        with suppress_outbound_history():
            await bot.send_message(
                case.requester_telegram_id,
                _(
                    "<b>Support case resolved</b>\n{kind}: <code>{reference}</code>"
                ).format(
                    kind=escape(_support_kind(case.kind)),
                    reference=escape(case.reference),
                ),
                protect_content=True,
            )
    except Exception as exc:
        report_exception(
            "operator.support_resolution_notification_failed",
            exception=exc,
            level="warning",
            operator_id=callback.from_user.id,
        )
        await _refresh_support_queue(
            message,
            support_requests=support_requests,
            operator_id=callback.from_user.id,
            notice=_("Notification failed. The case remains open."),
        )
        return

    try:
        result = await support_requests.resolve_operator(reference)
    except Exception as exc:
        report_exception(
            "operator.support_resolution_failed",
            exception=exc,
            operator_id=callback.from_user.id,
        )
        await _refresh_support_queue(
            message,
            support_requests=support_requests,
            operator_id=callback.from_user.id,
            notice=_(
                "The requester was notified, but closing failed. The case remains open."
            ),
        )
        return
    if result is None or not result.changed:
        await _refresh_support_queue(
            message,
            support_requests=support_requests,
            operator_id=callback.from_user.id,
            notice=_("The case was already resolved."),
        )
        return

    logfire.info(
        "operator.support_case_resolved",
        operator_id=callback.from_user.id,
    )
    await _refresh_support_queue(
        message,
        support_requests=support_requests,
        operator_id=callback.from_user.id,
        notice=_("Case resolved. The requester was notified."),
    )


@stale_callback_router.callback_query(F.data.startswith(_OPERATOR_CALLBACK_PREFIXES))
async def reject_stale_operator_callback(callback: CallbackQuery) -> None:
    """Clear malformed or obsolete operator buttons for authorized actors."""
    await callback.answer(
        _("This operator button expired. Open /operator again."),
        show_alert=True,
    )


@rejection_router.message(Command("operator", "ops"))
async def reject_unauthorized_operator_command(message: Message) -> None:
    """Consume privileged command names before the conversational catch-all."""
    await message.reply(_("This command isn't available."))


@rejection_router.callback_query(F.data.startswith(_OPERATOR_CALLBACK_PREFIXES))
async def reject_unauthorized_operator_callback(callback: CallbackQuery) -> None:
    """Always clear spinners for inaccessible or forwarded operator controls."""
    await callback.answer(_("This control is no longer available."), show_alert=True)


async def _private_operator_callback(callback: CallbackQuery) -> Message | None:
    message = callback.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != callback.from_user.id
    ):
        await callback.answer(
            _("Open the operator console in your private chat."),
            show_alert=True,
        )
        return None
    return message


async def _refresh_panel(
    message: Message,
    *,
    view: OperatorView,
    operator_console: OperatorConsoleService,
    operator_config: OperatorControlConfig,
    operator_id: int,
) -> None:
    try:
        snapshot = await operator_console.snapshot()
        text, markup = build_operator_panel(view, snapshot, operator_config)
    except Exception as exc:
        report_exception(
            "operator.panel_refresh_failed",
            exception=exc,
            operator_id=operator_id,
            view=view.value,
        )
        text = _("<b>Operator console unavailable</b>\nTry again.")
        markup = _section_navigation(OperatorView.OVERVIEW)
    await _edit_operator_message(
        message,
        text,
        markup,
        operator_id=operator_id,
        event="operator.panel_edit_failed",
    )


async def _refresh_support_queue(
    message: Message,
    *,
    support_requests: SupportRequestService,
    operator_id: int,
    notice: str | None = None,
) -> None:
    try:
        cases = await support_requests.list_operator_open()
        text, markup = build_operator_support_queue(cases, notice=notice)
    except Exception as exc:
        report_exception(
            "operator.support_queue_refresh_failed",
            exception=exc,
            operator_id=operator_id,
        )
        text = _("<b>Support queue unavailable</b>\nTry again.")
        markup = _support_queue_navigation()
    await _edit_operator_message(
        message,
        text,
        markup,
        operator_id=operator_id,
        event="operator.support_queue_render_failed",
    )


async def _edit_operator_message(
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    operator_id: int,
    event: str,
) -> None:
    try:
        with suppress_outbound_history():
            await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            report_exception(
                event,
                exception=exc,
                level="warning",
                operator_id=operator_id,
            )
    except TelegramAPIError as exc:
        report_exception(
            event,
            exception=exc,
            level="warning",
            operator_id=operator_id,
        )


def _overview_panel(
    snapshot: OperatorConsoleSnapshot,
    config: OperatorControlConfig,
) -> tuple[str, InlineKeyboardMarkup]:
    running = sum(worker.running for worker in snapshot.runtime.workers)
    database = snapshot.database
    database_state = (
        _("degraded")
        if database.is_degraded
        else _("ready · {latency} ms").format(
            latency=_number(round(database.latency_ms or 0))
        )
    )
    attention = _attention_count(snapshot)
    attention_state = (
        _("none")
        if attention == 0
        else _(
            "{count} signal",
            "{count} signals",
            attention,
        ).format(count=_number(attention))
    )
    text = _(
        "<b>Operator console</b>\n"
        "{environment} · up {uptime}\n\n"
        "Database: {database}\n"
        "Workers: {running}/{total}\n"
        "Attention: {attention}"
    ).format(
        environment=escape(config.environment),
        uptime=_duration(snapshot.runtime.uptime_seconds),
        database=database_state,
        running=running,
        total=len(snapshot.runtime.workers),
        attention=attention_state,
    )
    return text, InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _nav_button(_("Usage"), OperatorView.USAGE),
                _nav_button(_("Inference"), OperatorView.INFERENCE),
            ],
            [
                _nav_button(_("Commerce"), OperatorView.COMMERCE),
                _support_queue_button(_("Support")),
            ],
            [
                _nav_button(_("Operations"), OperatorView.OPERATIONS),
                _nav_button(_("Runtime"), OperatorView.RUNTIME),
            ],
            [
                _nav_button(_("Tests"), OperatorView.TESTS),
                _nav_button(_("Maintenance"), OperatorView.MAINTENANCE),
            ],
            [_nav_button(_("Refresh"), OperatorView.OVERVIEW)],
        ]
    )


def _usage_panel(
    snapshot: OperatorConsoleSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    db = _ready_database(snapshot)
    if db is None:
        return _database_unavailable_panel(_("Usage"), OperatorView.USAGE)
    users = _required(db.users, "users")
    chats = _required(db.chats, "chats")
    retained_messages = _required(db.retained_messages, "retained_messages")
    chats_by_type = _required(db.chats_by_type, "chats_by_type")
    artifacts = _required(db.artifacts, "artifacts")
    text = _(
        "<b>Usage</b>\n"
        "Users: {users} · {users_recent} changed / 24h\n"
        "Chats: {chats} · {chats_recent} changed / 24h\n"
        "Messages: {messages} retained · {messages_recent} / 24h\n\n"
        "Chat types: {chat_types}\n"
        "Artifacts: {artifacts} · {artifact_size}"
    ).format(
        users=_number(users.total),
        users_recent=_number(users.recent_24h),
        chats=_number(chats.total),
        chats_recent=_number(chats.recent_24h),
        messages=_number(retained_messages.total),
        messages_recent=_number(retained_messages.recent_24h),
        chat_types=_format_buckets(chats_by_type),
        artifacts=_number(artifacts.count),
        artifact_size=_bytes(artifacts.bytes),
    )
    return text, _section_navigation(OperatorView.USAGE)


def _commerce_panel(
    snapshot: OperatorConsoleSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    db = _ready_database(snapshot)
    if db is None:
        return _database_unavailable_panel(_("Commerce"), OperatorView.COMMERCE)
    wallet = _required(db.wallet, "wallet")
    stars = _required(db.stars, "stars")
    subscriptions = _required(db.subscriptions, "subscriptions")
    intent_states = _required(db.intent_states, "intent_states")
    receipt_states = _required(db.receipt_states, "receipt_states")
    support = _required(db.support, "support")
    payment_updates = _required(db.payment_updates, "payment_updates")
    refund_states = _required(db.refund_request_states, "refund_request_states")
    text = _(
        "<b>Commerce</b>\n"
        "Credits: {available} available · {reserved} reserved\n"
        "Consumed: {consumed} · debt: {debt}\n"
        "Stars: {fulfilled} fulfilled · {clawed_back} clawed back\n"
        "{economics}\n\n"
        "Subscriptions: {entitled} entitled · {renewing} renewing\n"
        "Renewal queue: {renewal_pending} pending · "
        "{renewal_processing} processing · {renewal_attention} attention · "
        "{renewal_due} due\n"
        "Support: {support_open} open · {support_payment} payment · "
        "{support_resolved} resolved\n"
        "Payment inbox: {payment_pending} pending · {payment_processing} processing · "
        "{payment_attention} attention · {payment_due} due\n"
        "Payment replies: {reply_failed} failed · {reply_skipped} skipped\n"
        "Refund requests: {refunds}\n"
        "Purchase intents: {intents}\n"
        "Receipts: {receipts}"
    ).format(
        available=_number(wallet.available_credits),
        reserved=_number(wallet.reserved_credits),
        consumed=_number(wallet.consumed_credits),
        debt=_number(wallet.debt_credits),
        fulfilled=_number(stars.fulfilled),
        clawed_back=_number(stars.clawed_back),
        economics=_economics_floor(snapshot, stars.fulfilled),
        entitled=_number(subscriptions.entitled),
        renewing=_number(subscriptions.auto_renewing),
        renewal_pending=_number(subscriptions.renewal_pending),
        renewal_processing=_number(subscriptions.renewal_processing),
        renewal_attention=_number(subscriptions.renewal_attention),
        renewal_due=_number(subscriptions.renewal_due),
        support_open=_number(support.open),
        support_payment=_number(support.payment_open),
        support_resolved=_number(support.resolved),
        payment_pending=_number(payment_updates.pending),
        payment_processing=_number(payment_updates.processing),
        payment_attention=_number(payment_updates.attention),
        payment_due=_number(payment_updates.due),
        reply_failed=_number(payment_updates.reply_failed),
        reply_skipped=_number(payment_updates.reply_skipped),
        refunds=_format_buckets(refund_states),
        intents=_format_buckets(intent_states),
        receipts=_format_buckets(receipt_states),
    )
    return text, _commerce_navigation()


def _economics_floor(snapshot: OperatorConsoleSnapshot, fulfilled_stars: int) -> str:
    revenue = (
        DEFAULT_STAR_ECONOMICS.planning_revenue_floor_per_star_usd * fulfilled_stars
    )
    usage = snapshot.inference and snapshot.inference.usage
    if usage is None:
        return _("Economics floor: {revenue} revenue · AI cost unavailable").format(
            revenue=_usd(revenue)
        )
    cost = usage.reconciled_cost_usd
    if usage.pending_cost_reconciliation or usage.unavailable_cost_count:
        return _(
            "Economics floor: {revenue} revenue · {cost} reconciled AI cost · "
            "margin incomplete ({pending} pending, {unavailable} unavailable)"
        ).format(
            revenue=_usd(revenue),
            cost=_usd(cost),
            pending=_number(usage.pending_cost_reconciliation),
            unavailable=_number(usage.unavailable_cost_count),
        )
    if revenue == 0:
        margin = _("unavailable")
    else:
        margin = f"{(revenue - cost) / revenue:.1%}"
    return _(
        "Economics floor: {revenue} revenue · {cost} recorded AI cost · {margin} margin"
    ).format(
        revenue=_usd(revenue),
        cost=_usd(cost),
        margin=margin,
    )


def _operations_panel(
    snapshot: OperatorConsoleSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    db = _ready_database(snapshot)
    if db is None:
        return _database_unavailable_panel(_("Operations"), OperatorView.OPERATIONS)
    operation_states = _required(db.operation_states, "operation_states")
    delivery_states = _required(db.delivery_states, "delivery_states")
    approval_states = _required(db.approval_states, "approval_states")
    text = _(
        "<b>Operations</b>\n"
        "Ledger: {operations}\n\n"
        "Delivery: {deliveries}\n\n"
        "Approvals: {approvals}"
    ).format(
        operations=_format_buckets(operation_states),
        deliveries=_format_buckets(delivery_states),
        approvals=_format_buckets(approval_states),
    )
    return text, _section_navigation(OperatorView.OPERATIONS)


def _inference_panel(
    snapshot: OperatorConsoleSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    inference = snapshot.inference
    if inference is None:
        return (
            _("<b>Inference</b>\nDiagnostics are unavailable."),
            _inference_navigation(),
        )

    usage = inference.usage
    if usage is None:
        usage_text = _("Usage diagnostics are unavailable.")
    else:
        attempts = usage.attempts
        tokens = usage.tokens
        usage_text = _(
            "Attempts: {recent} / 24h · {total} total\n"
            "States (24h/total): success {success_recent}/{success} · "
            "failed {failed_recent}/{failed} · pending {pending_recent}/{pending}\n"
            "Cost: {cost} reconciled · {cost_pending} pending · "
            "{cost_unavailable} unavailable\n\n"
            "Tokens\n"
            "Input {input} · output {output} · total {token_total}\n"
            "Cache read {cache_read} · write {cache_write}\n"
            "Reasoning {reasoning} · audio in/out {audio_input}/{audio_output}"
        ).format(
            recent=_number(attempts.recent_24h),
            total=_number(attempts.total),
            success_recent=_number(attempts.succeeded_24h),
            success=_number(attempts.succeeded),
            failed_recent=_number(attempts.failed_24h),
            failed=_number(attempts.failed),
            pending_recent=_number(attempts.pending_24h),
            pending=_number(attempts.pending),
            cost=_usd(usage.reconciled_cost_usd),
            cost_pending=_number(usage.pending_cost_reconciliation),
            cost_unavailable=_number(usage.unavailable_cost_count),
            input=_number(tokens.input),
            output=_number(tokens.output),
            token_total=_number(tokens.total),
            cache_read=_number(tokens.cache_read),
            cache_write=_number(tokens.cache_write),
            reasoning=_number(tokens.reasoning),
            audio_input=_number(tokens.audio_input),
            audio_output=_number(tokens.audio_output),
        )

    catalog = inference.catalog
    connectivity = inference.connectivity
    available_role_names = set(catalog.available_roles)
    unavailable_roles = tuple(
        role for role in catalog.enabled_roles if role not in available_role_names
    )
    text = _(
        "<b>Inference</b>\n"
        "{usage}\n\n"
        "Models\n"
        "Roles: {available}/{enabled} available · reviewed {reviewed}\n"
        "Available roles: {available_roles}\n"
        "Unavailable roles: {unavailable_roles}\n"
        "OpenRouter key: {balance}\n"
        "OpenRouter catalog: {provider_catalog}"
    ).format(
        usage=usage_text,
        available=_number(len(catalog.available_roles)),
        enabled=_number(len(catalog.enabled_roles)),
        reviewed=catalog.verified_on.isoformat(),
        available_roles=_format_roles(catalog.available_roles),
        unavailable_roles=_format_roles(unavailable_roles),
        balance=_inference_balance(connectivity),
        provider_catalog=_inference_catalog(connectivity, len(catalog.enabled_roles)),
    )
    return text, _inference_navigation()


def _runtime_panel(
    snapshot: OperatorConsoleSnapshot,
    config: OperatorControlConfig,
) -> tuple[str, InlineKeyboardMarkup]:
    db = _ready_database(snapshot)
    pool = _("unavailable")
    if db and db.pool:
        pool = _("{open}/{size} open · {overflow} overflow").format(
            open=_number(db.pool.open_connections),
            size=_number(db.pool.size),
            overflow=_number(db.pool.overflow),
        )
    workers = " · ".join(
        _("{worker} {state}").format(
            worker=_maintenance_label(worker.action),
            state=_("on") if worker.running else _("off"),
        )
        for worker in snapshot.runtime.workers
    )
    packages = " · ".join(
        f"{escape(name)} {escape(package_version)}"
        for name, package_version in _package_versions()
    )
    models = "\n".join(
        f"<code>{escape(key.value)}</code>: "
        f"<code>{escape(spec.provider_model_id)}</code>"
        for key, spec in GOOGLE_MODEL_CATALOG.items()
    )
    text = _(
        "<b>Runtime</b>\n"
        "Derp {version} · Python {python}\n"
        "{environment} · purchases {purchases} · AI content export {capture}\n"
        "DB pool: {pool}\n"
        "Workers: {workers}\n\n"
        "Libraries: {packages}\n"
        "Model catalog: {catalog_date}\n"
        "{models}"
    ).format(
        version=escape(config.service_version),
        python=escape(f"{sys.version_info.major}.{sys.version_info.minor}"),
        environment=escape(config.environment),
        purchases=_("on") if config.public_purchases_enabled else _("off"),
        capture=_("on") if config.ai_content_capture_enabled else _("off"),
        pool=pool,
        workers=workers,
        packages=packages,
        catalog_date=CATALOG_VERIFIED_ON.isoformat(),
        models=models,
    )
    return text, _section_navigation(OperatorView.RUNTIME)


def _maintenance_panel() -> tuple[str, InlineKeyboardMarkup]:
    text = _(
        "<b>Maintenance</b>\nRun an immediate pass through the live runtime workers."
    )
    return text, InlineKeyboardMarkup(
        inline_keyboard=[
            [_maintenance_button(_("All passes"), OperatorMaintenanceAction.ALL)],
            [
                _maintenance_button(
                    _("Subscriptions"), OperatorMaintenanceAction.SUBSCRIPTIONS
                ),
                _maintenance_button(_("Payments"), OperatorMaintenanceAction.PAYMENTS),
            ],
            [
                _maintenance_button(
                    _("Operations"), OperatorMaintenanceAction.OPERATIONS
                ),
                _maintenance_button(
                    _("Deliveries"), OperatorMaintenanceAction.DELIVERIES
                ),
            ],
            [
                _maintenance_button(_("History"), OperatorMaintenanceAction.HISTORY),
                _maintenance_button(
                    _("Approvals"), OperatorMaintenanceAction.APPROVALS
                ),
            ],
            [
                _maintenance_button(
                    _("Inference"), OperatorMaintenanceAction.INFERENCE
                ),
            ],
            [_nav_button(_("Back"), OperatorView.OVERVIEW)],
        ]
    )


def _tests_panel(
    snapshot: OperatorConsoleSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    database = _("degraded") if snapshot.database.is_degraded else _("ready")
    text = _(
        "<b>Tests</b>\n"
        "Database read: {database}\n"
        "Stars checkout: production intent and settlement path\n"
        "Command menu: all locales and audience scopes"
    ).format(database=database)
    return text, InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Open 1-Star checkout"),
                    callback_data=OperatorUtilityCallback(
                        action=OperatorUtility.TEST_PURCHASE
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Refund latest 1-Star test"),
                    callback_data=OperatorUtilityCallback(
                        action=OperatorUtility.TEST_REFUND
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Sync command menu"),
                    callback_data=OperatorUtilityCallback(
                        action=OperatorUtility.SYNC_COMMANDS
                    ).pack(),
                )
            ],
            [_nav_button(_("Back"), OperatorView.OVERVIEW)],
        ]
    )


def _database_unavailable_panel(
    title: str,
    view: OperatorView,
) -> tuple[str, InlineKeyboardMarkup]:
    return (
        _("<b>{title}</b>\nDatabase diagnostics are unavailable.").format(
            title=escape(title)
        ),
        _section_navigation(view),
    )


def _section_navigation(view: OperatorView) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _nav_button(_("Back"), OperatorView.OVERVIEW),
                _nav_button(_("Refresh"), view),
            ]
        ]
    )


def _commerce_navigation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Open support queue"),
                    callback_data=OperatorSupportQueueCallback(
                        action=OperatorSupportQueueAction.LIST
                    ).pack(),
                )
            ],
            [
                _nav_button(_("Back"), OperatorView.OVERVIEW),
                _nav_button(_("Refresh"), OperatorView.COMMERCE),
            ],
        ]
    )


def _support_queue_navigation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _nav_button(_("Back"), OperatorView.COMMERCE),
                InlineKeyboardButton(
                    text=_("Refresh"),
                    callback_data=OperatorSupportQueueCallback(
                        action=OperatorSupportQueueAction.LIST
                    ).pack(),
                ),
            ]
        ]
    )


def _inference_navigation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Run read-only check"),
                    callback_data=OperatorUtilityCallback(
                        action=OperatorUtility.INFERENCE_CHECK
                    ).pack(),
                )
            ],
            [
                _nav_button(_("Back"), OperatorView.OVERVIEW),
                _nav_button(_("Refresh"), OperatorView.INFERENCE),
            ],
        ]
    )


def _nav_button(text: str, view: OperatorView) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=OperatorNavigationCallback(view=view).pack(),
    )


def _support_queue_button(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=OperatorSupportQueueCallback(
            action=OperatorSupportQueueAction.LIST
        ).pack(),
    )


def _maintenance_button(
    text: str,
    action: OperatorMaintenanceAction,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=OperatorMaintenanceCallback(action=action).pack(),
    )


def _ready_database(
    snapshot: OperatorConsoleSnapshot,
) -> OperatorDatabaseSnapshot | None:
    return None if snapshot.database.is_degraded else snapshot.database


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise RuntimeError(f"ready operator snapshot omitted {name}")
    return value


def _attention_count(snapshot: OperatorConsoleSnapshot) -> int:
    db = _ready_database(snapshot)
    if db is None:
        return 1
    intent_states = _required(db.intent_states, "intent_states")
    receipt_states = _required(db.receipt_states, "receipt_states")
    operation_states = _required(db.operation_states, "operation_states")
    delivery_states = _required(db.delivery_states, "delivery_states")
    wallet = _required(db.wallet, "wallet")
    support = _required(db.support, "support")
    payment_updates = _required(db.payment_updates, "payment_updates")
    subscriptions = _required(db.subscriptions, "subscriptions")
    refund_states = _required(db.refund_request_states, "refund_request_states")
    inference = snapshot.inference and snapshot.inference.usage
    return (
        _bucket_count(intent_states, "needs_review")
        + _bucket_count(receipt_states, "needs_review")
        + _bucket_count(operation_states, "failed")
        + _bucket_count(delivery_states, "failed")
        + _bucket_count(delivery_states, "uncertain")
        + int(wallet.debt_credits > 0)
        + support.open
        + payment_updates.attention
        + payment_updates.due
        + payment_updates.reply_failed
        + payment_updates.reply_skipped
        + subscriptions.renewal_attention
        + subscriptions.renewal_due
        + _bucket_count(refund_states, "accepted")
        + _bucket_count(refund_states, "needs_review")
        + (inference.route_policy_violation_count if inference else 0)
        + sum(not worker.running for worker in snapshot.runtime.workers)
    )


def _bucket_count(buckets: tuple[OperatorNamedCount, ...], name: str) -> int:
    return next((bucket.count for bucket in buckets if bucket.name == name), 0)


def _format_buckets(buckets: tuple[OperatorNamedCount, ...]) -> str:
    visible = [
        _("{state} {count}").format(
            state=_state_label(bucket.name),
            count=_number(bucket.count),
        )
        for bucket in buckets
        if bucket.count
    ]
    return " · ".join(visible) if visible else _("none")


def _format_roles(roles: tuple[str, ...]) -> str:
    if not roles:
        return _("none")
    return " · ".join(f"<code>{escape(role)}</code>" for role in roles)


def _state_label(name: str) -> str:
    labels = {
        "private": _("private"),
        "group": _("group"),
        "supergroup": _("supergroup"),
        "channel": _("channel"),
        "quoted": _("quoted"),
        "reserved": _("reserved"),
        "executing": _("executing"),
        "captured": _("captured"),
        "released": _("released"),
        "reversed": _("reversed"),
        "canceled": _("canceled"),
        "failed": _("failed"),
        "not_ready": _("not ready"),
        "pending": _("pending"),
        "delivering": _("delivering"),
        "delivered": _("delivered"),
        "uncertain": _("uncertain"),
        "expired": _("expired"),
        "approved": _("approved"),
        "denied": _("denied"),
        "resumed": _("resumed"),
        "prechecked": _("pre-checked"),
        "fulfilled": _("fulfilled"),
        "needs_review": _("needs review"),
        "received": _("received"),
        "clawed_back": _("clawed back"),
        "active": _("active"),
    }
    return labels.get(name, name.replace("_", " "))


def _maintenance_label(action: OperatorMaintenanceAction) -> str:
    return {
        OperatorMaintenanceAction.ALL: _("All passes"),
        OperatorMaintenanceAction.HISTORY: _("History"),
        OperatorMaintenanceAction.SUBSCRIPTIONS: _("Subscriptions"),
        OperatorMaintenanceAction.PAYMENTS: _("Payments"),
        OperatorMaintenanceAction.OPERATIONS: _("Operations"),
        OperatorMaintenanceAction.DELIVERIES: _("Deliveries"),
        OperatorMaintenanceAction.APPROVALS: _("Approvals"),
        OperatorMaintenanceAction.INFERENCE: _("Inference"),
    }[action]


def _maintenance_count_label(name: str) -> str:
    labels = {
        "purged_message_count": _("Purged messages"),
        "expired_cycle_count": _("Expired cycles"),
        "payment_update_claimed_count": _("Payment updates claimed"),
        "payment_update_settled_count": _("Payment updates settled"),
        "payment_update_attention_count": _("Payment updates needing attention"),
        "payment_update_retry_scheduled_count": _("Payment update retries"),
        "payment_reply_sent_count": _("Payment replies sent"),
        "payment_reply_failed_count": _("Payment replies failed"),
        "payment_reply_skipped_count": _("Payment replies skipped"),
        "debug_refund_processed_count": _("Test refunds processed"),
        "debug_refund_reconciled_count": _("Test refunds reconciled"),
        "debug_refund_pending_review_count": _("Test refunds needing review"),
        "examined_count": _("Examined operations"),
        "expired_quote_count": _("Expired quotes"),
        "released_reservation_count": _("Released reservations"),
        "released_execution_count": _("Released executions"),
        "recovered_delivery_count": _("Recovered deliveries"),
        "incomplete_result_count": _("Incomplete results"),
        "race_skipped_count": _("Race skips"),
        "interrupted_count": _("Interrupted deliveries"),
        "retry_candidate_count": _("Retry candidates"),
        "delivered_count": _("Delivered"),
        "retryable_failure_count": _("Retryable failures"),
        "terminal_failure_count": _("Terminal failures"),
        "uncertain_count": _("Uncertain deliveries"),
        "retry_exception_count": _("Retry exceptions"),
        "expired_count": _("Expired deliveries"),
        "interruption_failure_count": _("Interruption failures"),
        "expiration_failure_count": _("Expiration failures"),
        "artifact_examined_count": _("Examined artifacts"),
        "artifact_purged_count": _("Purged artifacts"),
        "artifact_failure_count": _("Artifact failures"),
        "configured_count": _("Configured"),
        "claimed_count": _("Claims"),
        "reconciled_count": _("Reconciled costs"),
        "retry_scheduled_count": _("Retries scheduled"),
        "unavailable_count": _("Metadata unavailable"),
        "claim_lost_count": _("Claims lost"),
        "stale_attempt_count": _("Stale attempts closed"),
        "phase_failure_count": _("Phase failures"),
        "expired_request_count": _("Expired approvals"),
    }
    return labels.get(name, name.replace("_", " "))


def _support_kind(kind: SupportKind) -> str:
    return {
        SupportKind.PAYMENT: _("Payment or credits"),
        SupportKind.REFUND: _("Refund"),
        SupportKind.PRIVACY: _("Privacy or data"),
        SupportKind.ACCESS: _("Access or account"),
    }[kind]


def _support_created_at(case: OperatorSupportCase) -> str:
    if case.created_at.tzinfo is None or case.created_at.utcoffset() is None:
        raise ValueError("support case creation time must be timezone-aware")
    return case.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _number(value: int) -> str:
    return format_local_integer(value)


def _usd(value: Decimal | None) -> str:
    if value is None:
        raise RuntimeError("ready inference balance omitted a value")
    rendered = f"{value:,.6f}".rstrip("0").rstrip(".")
    return f"${rendered}"


def _inference_balance(snapshot: OperatorInferenceConnectivitySnapshot) -> str:
    if snapshot.balance_status is OperatorProbeStatus.READY:
        if snapshot.key_limit_usd is None:
            return _("{used} used · no key limit").format(
                used=_usd(snapshot.key_usage_usd)
            )
        return _("{remaining} of {limit} left · {used} used").format(
            remaining=_usd(snapshot.key_remaining_usd),
            limit=_usd(snapshot.key_limit_usd),
            used=_usd(snapshot.key_usage_usd),
        )
    return _probe_status(snapshot.balance_status)


def _inference_catalog(
    snapshot: OperatorInferenceConnectivitySnapshot,
    enabled_roles: int,
) -> str:
    if snapshot.catalog_status is OperatorProbeStatus.READY:
        return _("{count} models · {visible}/{enabled} enabled visible").format(
            count=_number(snapshot.catalog_model_count or 0),
            visible=_number(len(snapshot.visible_enabled_roles)),
            enabled=_number(enabled_roles),
        )
    return _probe_status(snapshot.catalog_status)


def _probe_status(status: OperatorProbeStatus) -> str:
    return {
        OperatorProbeStatus.NOT_CONFIGURED: _("not configured"),
        OperatorProbeStatus.NOT_CHECKED: _("not checked"),
        OperatorProbeStatus.READY: _("ready"),
        OperatorProbeStatus.FAILED: _("unavailable"),
    }[status]


def _duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    days, remainder = divmod(whole, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return _("{days}d {hours}h").format(days=days, hours=hours)
    if hours:
        return _("{hours}h {minutes}m").format(hours=hours, minutes=minutes)
    if minutes:
        return _("{minutes}m {seconds}s").format(
            minutes=minutes,
            seconds=seconds,
        )
    return _("{seconds}s").format(seconds=seconds)


def _bytes(value: int) -> str:
    size = float(value)
    units = (_("B"), _("KiB"), _("MiB"), _("GiB"), _("TiB"))
    for index, unit in enumerate(units):
        if size < 1_024 or index == len(units) - 1:
            return f"{size:.0f} {unit}" if index == 0 else f"{size:.1f} {unit}"
        size /= 1_024
    raise AssertionError("unreachable")


def _package_versions() -> tuple[tuple[str, str], ...]:
    packages = (
        ("aiogram", "aiogram"),
        ("pydantic-ai", "pydantic-ai"),
        ("logfire", "logfire"),
        ("SQLAlchemy", "SQLAlchemy"),
    )
    resolved: list[tuple[str, str]] = []
    for label, distribution in packages:
        try:
            package_version = version(distribution)
        except PackageNotFoundError:
            package_version = _("unknown")
        resolved.append((label, package_version))
    return tuple(resolved)


router.include_routers(purchase_test_router, stale_callback_router)


__all__ = [
    "OperatorSupportAction",
    "OperatorSupportQueueCallback",
    "OperatorSupportResolveCallback",
    "OperatorSupportResolveConfirmCallback",
    "OperatorMaintenanceCallback",
    "OperatorMaintenanceConfirmCallback",
    "OperatorDebugRefundConfirmCallback",
    "OperatorNavigationCallback",
    "OperatorUtility",
    "OperatorUtilityCallback",
    "OperatorView",
    "build_maintenance_result_panel",
    "build_operator_panel",
    "build_operator_support_queue",
    "purchase_test_router",
    "rejection_router",
    "router",
    "stale_callback_router",
]
