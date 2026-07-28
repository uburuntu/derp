"""Published legal terms and durable in-bot support intake."""

from __future__ import annotations

from enum import StrEnum
from html import escape

import logfire
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import gettext as _

from derp.history.capture import suppress_outbound_history
from derp.legal import (
    PRIVACY_POLICY_URL,
    TERMS_ACCEPTANCE_VERSION,
    TERMS_OF_USE_URL,
)
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operator import OperatorControlConfig
from derp.support import (
    SupportCapacityError,
    SupportCase,
    SupportKind,
    SupportRequestService,
    SupportSource,
    TermsAcceptanceService,
)

router = Router(name="legal_support")


class TermsAcceptanceSource(StrEnum):
    TERMS_COMMAND = "terms_command"
    PURCHASE_GATE = "purchase_gate"


class TermsAcceptCallback(CallbackData, prefix="terms"):
    version: str
    source: TermsAcceptanceSource


class SupportCreateCallback(CallbackData, prefix="support"):
    kind: SupportKind
    source: SupportSource


SUPPORT_MENU_CALLBACK = "support:menu"


def build_terms_panel(
    *,
    accepted: bool,
    source: TermsAcceptanceSource = TermsAcceptanceSource.TERMS_COMMAND,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the published documents and explicit current-version acceptance."""
    state = (
        _("Accepted for purchases")
        if accepted
        else _("Accept these terms before buying credits or a plan.")
    )
    rows = [
        [
            InlineKeyboardButton(text=_("Terms of use"), url=TERMS_OF_USE_URL),
            InlineKeyboardButton(text=_("Privacy policy"), url=PRIVACY_POLICY_URL),
        ]
    ]
    if not accepted:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("Accept terms"),
                    callback_data=TermsAcceptCallback(
                        version=TERMS_ACCEPTANCE_VERSION,
                        source=source,
                    ).pack(),
                )
            ]
        )
    return (
        _("<b>Terms and privacy</b>\n{state}").format(state=state),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def build_support_panel(
    cases: tuple[SupportCase, ...] = (),
    *,
    source: SupportSource = SupportSource.SUPPORT,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render category-only intake and the actor's open opaque references."""
    open_lines = [
        _("{kind}: <code>{reference}</code>").format(
            kind=_support_kind(case.kind),
            reference=escape(case.reference),
        )
        for case in cases
    ]
    current = (
        _("\n\nOpen cases\n{cases}").format(cases="\n".join(open_lines))
        if open_lines
        else ""
    )
    text = _(
        "<b>Support</b>\nChoose the closest category. Derp stores the category "
        "and case reference, not a free-form support message.{current}"
    ).format(current=current)
    rows = [
        [
            _support_button(SupportKind.PAYMENT, source),
            _support_button(SupportKind.REFUND, source),
        ],
        [
            _support_button(SupportKind.PRIVACY, source),
            _support_button(SupportKind.ACCESS, source),
        ],
        [
            InlineKeyboardButton(text=_("Terms of use"), url=TERMS_OF_USE_URL),
            InlineKeyboardButton(text=_("Privacy policy"), url=PRIVACY_POLICY_URL),
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("terms"))
async def show_terms(
    message: Message,
    terms_acceptance: TermsAcceptanceService,
    user_model: UserModel | None = None,
) -> None:
    """Show immutable legal links and the actor's purchase acceptance state."""
    if not _private_actor_message(message) or user_model is None:
        with suppress_outbound_history():
            await message.reply(_("Open Derp privately and use /terms."))
        return
    accepted = await terms_acceptance.has_current(user_model.id)
    text, markup = build_terms_panel(accepted=accepted)
    with suppress_outbound_history():
        await message.answer(text, reply_markup=markup, protect_content=True)


@router.callback_query(TermsAcceptCallback.filter())
async def accept_terms(
    query: CallbackQuery,
    callback_data: TermsAcceptCallback,
    terms_acceptance: TermsAcceptanceService,
    user_model: UserModel | None = None,
) -> None:
    """Persist one explicit acceptance before any public invoice can exist."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /terms."), show_alert=True)
        return
    if (
        user_model.telegram_id != query.from_user.id
        or callback_data.version != TERMS_ACCEPTANCE_VERSION
    ):
        await query.answer(
            _("These terms changed. Open /terms and review the current version."),
            show_alert=True,
        )
        return
    await terms_acceptance.accept_current(
        user_model.id,
        source=callback_data.source.value,
    )
    text, markup = build_terms_panel(accepted=True)
    if isinstance(query.message, Message):
        with suppress_outbound_history():
            await query.message.edit_text(text, reply_markup=markup)
    await query.answer(_("Terms accepted"))
    logfire.info(
        "legal.terms_accepted",
        terms_version=TERMS_ACCEPTANCE_VERSION,
        user_id=user_model.telegram_id,
    )


@router.message(Command("support"))
async def show_support(
    message: Message,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Open private category-only support intake."""
    if not _private_actor_message(message) or user_model is None:
        with suppress_outbound_history():
            await message.reply(_("Open Derp privately and use /support."))
        return
    cases = await support_requests.list_open(user_model.id)
    text, markup = build_support_panel(cases, source=SupportSource.SUPPORT)
    with suppress_outbound_history():
        await message.answer(text, reply_markup=markup, protect_content=True)


@router.message(Command("paysupport"))
async def open_payment_support(
    message: Message,
    support_requests: SupportRequestService,
    bot: Bot,
    operator_config: OperatorControlConfig,
    user_model: UserModel | None = None,
) -> None:
    """Open or recall the actor's bounded payment-support case."""
    if not _private_actor_message(message) or user_model is None:
        with suppress_outbound_history():
            await message.reply(_("Open Derp privately and use /paysupport."))
        return
    await _open_case(
        message=message,
        support_requests=support_requests,
        bot=bot,
        operator_config=operator_config,
        user_model=user_model,
        kind=SupportKind.PAYMENT,
        source=SupportSource.PAY_SUPPORT,
    )


@router.callback_query(F.data == SUPPORT_MENU_CALLBACK)
async def navigate_support(
    query: CallbackQuery,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Open support from privacy controls without creating a case."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /support."), show_alert=True)
        return
    cases = await support_requests.list_open(user_model.id)
    text, markup = build_support_panel(cases, source=SupportSource.PRIVACY)
    if isinstance(query.message, Message):
        with suppress_outbound_history():
            await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(SupportCreateCallback.filter())
async def create_support_case(
    query: CallbackQuery,
    callback_data: SupportCreateCallback,
    support_requests: SupportRequestService,
    bot: Bot,
    operator_config: OperatorControlConfig,
    user_model: UserModel | None = None,
) -> None:
    """Create a deduplicated content-free case and notify configured operators."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /support."), show_alert=True)
        return
    if user_model.telegram_id != query.from_user.id:
        await query.answer(_("This support button expired."), show_alert=True)
        return
    try:
        result = await support_requests.open(
            user_model.id,
            kind=callback_data.kind,
            source=callback_data.source,
        )
    except SupportCapacityError:
        await query.answer(
            _("You already have several open cases. Use /support to check them."),
            show_alert=True,
        )
        return
    if result.created:
        await _notify_operators(bot, operator_config, result.case)
    cases = await support_requests.list_open(user_model.id)
    text, markup = build_support_panel(cases, source=callback_data.source)
    if isinstance(query.message, Message):
        with suppress_outbound_history():
            await query.message.edit_text(text, reply_markup=markup)
    await query.answer(_("Case opened") if result.created else _("Case already open"))


async def _open_case(
    *,
    message: Message,
    support_requests: SupportRequestService,
    bot: Bot,
    operator_config: OperatorControlConfig,
    user_model: UserModel,
    kind: SupportKind,
    source: SupportSource,
) -> None:
    try:
        result = await support_requests.open(
            user_model.id,
            kind=kind,
            source=source,
        )
    except SupportCapacityError:
        with suppress_outbound_history():
            await message.answer(
                _("You already have several open cases. Use /support to check them."),
                protect_content=True,
            )
        return
    if result.created:
        await _notify_operators(bot, operator_config, result.case)
    cases = await support_requests.list_open(user_model.id)
    text, markup = build_support_panel(cases, source=source)
    with suppress_outbound_history():
        await message.answer(text, reply_markup=markup, protect_content=True)


async def _notify_operators(
    bot: Bot,
    config: OperatorControlConfig,
    case: SupportCase,
) -> None:
    notice = _(
        "<b>New support case</b>\nType: {kind}\nReference: <code>{reference}</code>"
    ).format(kind=_support_kind(case.kind), reference=escape(case.reference))
    for operator_id in sorted(config.operator_ids):
        try:
            with suppress_outbound_history():
                await bot.send_message(
                    operator_id,
                    notice,
                    protect_content=True,
                )
        except Exception as exc:
            report_exception(
                "support.operator_notification_failed",
                exception=exc,
                level="warning",
                operator_id=operator_id,
            )


def _support_button(
    kind: SupportKind,
    source: SupportSource,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=_support_kind(kind),
        callback_data=SupportCreateCallback(kind=kind, source=source).pack(),
    )


def _support_kind(kind: SupportKind) -> str:
    return {
        SupportKind.PAYMENT: _("Payment or credits"),
        SupportKind.REFUND: _("Refund"),
        SupportKind.PRIVACY: _("Privacy or data"),
        SupportKind.ACCESS: _("Access or account"),
    }[kind]


def _private_actor_message(message: Message) -> bool:
    return bool(
        message.chat.type == "private"
        and message.from_user
        and message.chat.id == message.from_user.id
    )


def _private_actor_callback(query: CallbackQuery) -> bool:
    return bool(
        isinstance(query.message, Message)
        and query.message.chat.type == "private"
        and query.message.chat.id == query.from_user.id
    )


__all__ = [
    "SUPPORT_MENU_CALLBACK",
    "SupportCreateCallback",
    "TermsAcceptCallback",
    "TermsAcceptanceSource",
    "build_support_panel",
    "build_terms_panel",
    "router",
]
