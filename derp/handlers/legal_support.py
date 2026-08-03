"""Published legal terms and durable in-bot support intake."""

from __future__ import annotations

from enum import StrEnum
from html import escape
from uuid import UUID

import logfire
from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.formatting import Bold, ExpandableBlockQuote, Text
from aiogram.utils.i18n import gettext as _

from derp.billing.telegram import PurchaseCallback, PurchaseTermsAcceptCallback
from derp.common.legal_documents import (
    LegalDocumentKind,
    LegalDocumentPage,
    legal_document_pages,
)
from derp.config import settings
from derp.db import DatabaseManager, remove_disqualified_message
from derp.history.capture import suppress_outbound_history
from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operator import OperatorControlConfig
from derp.support import (
    SupportCapacityError,
    SupportCase,
    SupportIntakeLookup,
    SupportIntakeLookupState,
    SupportKind,
    SupportPayment,
    SupportReceiptError,
    SupportRequestService,
    SupportSource,
    SupportStatus,
    SupportStatusMessage,
    TermsAcceptanceService,
)
from derp.support.telegram import (
    SupportStatusCallback,
    build_support_status_receipt,
)

router = Router(name="legal_support")


class TermsAcceptanceSource(StrEnum):
    TERMS_COMMAND = "terms_command"
    PURCHASE_GATE = "purchase_gate"


class TermsAcceptCallback(CallbackData, prefix="terms"):
    version: str
    source: TermsAcceptanceSource
    actor_id: int = 0


class LegalDocumentCallback(CallbackData, prefix="legal"):
    """Open one bounded page, optionally bound to its initiating actor."""

    document: LegalDocumentKind
    page: int = 0
    actor_id: int = 0
    separate: bool = False


class LegalCloseCallback(CallbackData, prefix="legal-close"):
    """Close a separately opened reader and reveal its untouched source panel."""

    actor_id: int = 0


class SupportCreateCallback(CallbackData, prefix="support"):
    kind: SupportKind
    source: SupportSource


class SupportPaymentCallback(CallbackData, prefix="support-pay"):
    kind: SupportKind
    source: SupportSource
    receipt_id: str


class SupportPaymentPageCallback(CallbackData, prefix="support-page"):
    kind: SupportKind
    source: SupportSource
    offset: int = 0


class SupportMenuCallback(CallbackData, prefix="support-menu"):
    """Return to the support panel without losing its originating surface."""

    source: SupportSource


class SupportReplyFilter(BaseFilter):
    """Match one durable support prompt independently of its visible copy."""

    async def __call__(
        self,
        message: Message,
        support_requests: SupportRequestService,
    ) -> bool | dict[str, SupportIntakeLookup]:
        reply = message.reply_to_message
        if reply is None or message.chat.type != "private" or message.from_user is None:
            return False
        lookup = await support_requests.lookup_intake_for_telegram_user(
            message.from_user.id,
            prompt_chat_id=message.chat.id,
            prompt_message_id=reply.message_id,
        )
        if lookup.state is SupportIntakeLookupState.MISSING:
            if (
                reply.from_user is None
                or not reply.from_user.is_bot
                or reply.text != support_intake_prompt()
            ):
                return False
            lookup = SupportIntakeLookup(SupportIntakeLookupState.EXPIRED)
        return {"support_intake_lookup": lookup}


class SupportDeepLinkFilter(BaseFilter):
    """Match the private `/start support` deep link from a group."""

    async def __call__(self, message: Message) -> bool:
        parts = (message.text or "").split(maxsplit=1)
        command = parts[0].split("@", maxsplit=1)[0].casefold() if parts else ""
        return bool(
            message.chat.type == "private"
            and command == "/start"
            and len(parts) == 2
            and parts[1].casefold() == "support"
        )


SUPPORT_MENU_CALLBACK = SupportMenuCallback(source=SupportSource.PRIVACY).pack()
SUPPORT_PRIVACY_BACK_CALLBACK = "privacy:return"


def support_intake_prompt() -> str:
    """Return one concise prompt whose context lives in durable intake state."""
    return _("What happened? One message is enough.")


def build_terms_panel(
    *,
    accepted: bool,
    source: TermsAcceptanceSource = TermsAcceptanceSource.TERMS_COMMAND,
    actor_telegram_id: int = 0,
    pending_purchase: PurchaseCallback | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the published documents and explicit current-version acceptance."""
    state = (
        _("Accepted for purchases")
        if accepted
        else _("Accept these terms before buying credits or a plan.")
    )
    rows = [
        [
            _legal_button(
                LegalDocumentKind.TERMS,
                actor_telegram_id=actor_telegram_id,
            ),
            _legal_button(
                LegalDocumentKind.PRIVACY,
                actor_telegram_id=actor_telegram_id,
            ),
        ]
    ]
    if not accepted:
        acceptance = (
            PurchaseTermsAcceptCallback(
                version=terms_callback_version(),
                kind=pending_purchase.kind,
                product_id=pending_purchase.product_id,
                target=pending_purchase.target,
                actor_id=actor_telegram_id,
            ).pack()
            if pending_purchase is not None
            else TermsAcceptCallback(
                version=TERMS_ACCEPTANCE_VERSION,
                source=source,
                actor_id=actor_telegram_id,
            ).pack()
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("Accept terms"),
                    callback_data=acceptance,
                )
            ]
        )
    return (
        _("<b>Terms and privacy</b>\n{state}").format(state=state),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def build_legal_document_page(
    page: LegalDocumentPage,
    *,
    actor_telegram_id: int = 0,
) -> tuple[Text, InlineKeyboardMarkup]:
    """Render one collapsed page with bounded navigation controls."""
    content = Text(
        Bold(page.title),
        "\n",
        page.version,
        "\n",
        _("Page {page} of {total}").format(page=page.number, total=page.total),
        "\n\n",
        ExpandableBlockQuote(page.body),
    )
    navigation: list[InlineKeyboardButton] = []
    if page.number > 1:
        navigation.append(
            InlineKeyboardButton(
                text=_("Previous"),
                callback_data=LegalDocumentCallback(
                    document=page.document,
                    page=page.number - 2,
                    actor_id=actor_telegram_id,
                    separate=False,
                ).pack(),
            )
        )
    if page.number < page.total:
        navigation.append(
            InlineKeyboardButton(
                text=_("Next"),
                callback_data=LegalDocumentCallback(
                    document=page.document,
                    page=page.number,
                    actor_id=actor_telegram_id,
                    separate=False,
                ).pack(),
            )
        )
    rows = [navigation] if navigation else []
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Back"),
                callback_data=LegalCloseCallback(actor_id=actor_telegram_id).pack(),
            )
        ]
    )
    return content, InlineKeyboardMarkup(inline_keyboard=rows)


def terms_callback_version() -> str:
    """Return the compact current Terms coordinate used in callback data."""
    prefix = "derp-terms-"
    if not TERMS_ACCEPTANCE_VERSION.startswith(prefix):
        raise ValueError("Terms acceptance version has no callback-safe prefix")
    return TERMS_ACCEPTANCE_VERSION.removeprefix(prefix)


def _legal_button(
    document: LegalDocumentKind,
    *,
    actor_telegram_id: int = 0,
) -> InlineKeyboardButton:
    label = (
        _("Terms of use")
        if document is LegalDocumentKind.TERMS
        else _("Privacy policy")
    )
    return InlineKeyboardButton(
        text=label,
        callback_data=LegalDocumentCallback(
            document=document,
            actor_id=actor_telegram_id,
            separate=True,
        ).pack(),
    )


def build_support_panel(
    cases: tuple[SupportCase, ...] = (),
    *,
    source: SupportSource = SupportSource.SUPPORT,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render concise one-shot intake and the actor's open references."""
    open_lines = [
        _("{kind}: <code>{reference}</code> · {status}").format(
            kind=_support_kind(case.kind),
            reference=escape(case.reference),
            status=_support_status(case.status),
        )
        for case in cases
    ]
    current = (
        _("\n\nOpen cases\n{cases}").format(cases="\n".join(open_lines))
        if open_lines
        else ""
    )
    text = _(
        "<b>Support</b>\nChoose a topic, then send one short message.{current}"
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
            _legal_button(LegalDocumentKind.TERMS),
            _legal_button(LegalDocumentKind.PRIVACY),
        ],
    ]
    if source is SupportSource.PRIVACY:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("Back"),
                    callback_data=SUPPORT_PRIVACY_BACK_CALLBACK,
                )
            ]
        )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_support_payment_panel(
    payments: tuple[SupportPayment, ...],
    *,
    kind: SupportKind,
    source: SupportSource,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render recent receipts without exposing provider charge identifiers."""
    rows = []
    for payment in payments:
        credit_label = (
            _("{count} credits").format(count=payment.credits)
            if payment.credits is not None
            else _("credits")
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("{stars} Stars · {credits} · {date}").format(
                        stars=payment.stars,
                        credits=credit_label,
                        date=payment.created_at.strftime("%Y-%m-%d"),
                    ),
                    callback_data=SupportPaymentCallback(
                        kind=kind,
                        source=source,
                        receipt_id=payment.receipt_id.hex,
                    ).pack(),
                )
            ]
        )
    navigation = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                text=_("Previous"),
                callback_data=SupportPaymentPageCallback(
                    kind=kind,
                    source=source,
                    offset=max(0, offset - 5),
                ).pack(),
            )
        )
    if len(payments) == 5:
        navigation.append(
            InlineKeyboardButton(
                text=_("Next"),
                callback_data=SupportPaymentPageCallback(
                    kind=kind,
                    source=source,
                    offset=offset + 5,
                ).pack(),
            )
        )
    if navigation:
        rows.append(navigation)
    if kind is SupportKind.PAYMENT:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("Something else"),
                    callback_data=SupportPaymentCallback(
                        kind=kind,
                        source=source,
                        receipt_id="none",
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Back"),
                callback_data=SupportMenuCallback(source=source).pack(),
            )
        ]
    )
    text = (
        _("<b>Choose the payment</b>\nWhich one should I check?")
        if payments
        else _("<b>No payments found</b>\nThere is nothing refundable here yet.")
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("terms"))
async def show_terms(
    message: Message,
    terms_acceptance: TermsAcceptanceService,
    user_model: UserModel | None = None,
) -> None:
    """Show the in-bot legal reader and the actor's purchase acceptance state."""
    if message.from_user is None or user_model is None:
        with suppress_outbound_history():
            await message.reply(_("I couldn't find your account. Try again."))
        return
    if user_model.telegram_id != message.from_user.id:
        return
    accepted = await terms_acceptance.has_current(user_model.id)
    text, markup = build_terms_panel(
        accepted=accepted,
        actor_telegram_id=user_model.telegram_id,
    )
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
    if not isinstance(query.message, Message) or user_model is None:
        await query.answer(_("Open /terms and try again."), show_alert=True)
        return
    if (
        user_model.telegram_id != query.from_user.id
        or callback_data.actor_id not in {0, query.from_user.id}
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
    text, markup = build_terms_panel(
        accepted=True,
        source=callback_data.source,
        actor_telegram_id=query.from_user.id,
    )
    if isinstance(query.message, Message):
        with suppress_outbound_history():
            await query.message.edit_text(text, reply_markup=markup)
    await query.answer(_("Terms accepted"))
    logfire.info(
        "legal.terms_accepted",
        terms_version=TERMS_ACCEPTANCE_VERSION,
        user_id=user_model.telegram_id,
    )


@router.callback_query(LegalDocumentCallback.filter())
async def show_legal_document(
    query: CallbackQuery,
    callback_data: LegalDocumentCallback,
    user_model: UserModel | None = None,
) -> None:
    """Open a complete localized legal document without leaving Telegram."""
    if not isinstance(query.message, Message) or user_model is None:
        await query.answer(_("Open /terms and try again."), show_alert=True)
        return
    if callback_data.actor_id not in {0, query.from_user.id}:
        await query.answer(
            _("This legal panel belongs to someone else."), show_alert=True
        )
        return
    pages = legal_document_pages(
        callback_data.document,
        query.from_user.language_code or user_model.language_code or "en",
    )
    if not 0 <= callback_data.page < len(pages):
        await query.answer(_("This page expired. Open /terms again."), show_alert=True)
        return
    content, markup = build_legal_document_page(
        pages[callback_data.page],
        actor_telegram_id=callback_data.actor_id or query.from_user.id,
    )
    with suppress_outbound_history():
        if callback_data.separate:
            await query.message.answer(
                **content.as_kwargs(),
                reply_markup=markup,
                protect_content=True,
            )
        else:
            await query.message.edit_text(**content.as_kwargs(), reply_markup=markup)
    await query.answer()


@router.callback_query(LegalCloseCallback.filter())
async def close_legal_document(
    query: CallbackQuery,
    callback_data: LegalCloseCallback,
) -> None:
    """Close only the reader message; the purchase or settings panel remains."""
    if not isinstance(query.message, Message):
        await query.answer()
        return
    if callback_data.actor_id not in {0, query.from_user.id}:
        await query.answer(
            _("This legal panel belongs to someone else."), show_alert=True
        )
        return
    with suppress_outbound_history():
        await query.message.delete()
    await query.answer()


@router.message(Command("support"))
async def show_support(
    message: Message,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Open private category-only support intake."""
    if not _private_actor_message(message) or user_model is None:
        username = settings.bot_username.removeprefix("@")
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_("Open support"),
                        url=f"https://t.me/{username}?start=support",
                    )
                ]
            ]
        )
        with suppress_outbound_history():
            await message.reply(
                _("Support notes and payment details stay private."),
                reply_markup=markup,
            )
        return
    cases = await support_requests.list_open(user_model.id)
    text, markup = build_support_panel(cases, source=SupportSource.SUPPORT)
    with suppress_outbound_history():
        await message.answer(text, reply_markup=markup, protect_content=True)


@router.message(SupportDeepLinkFilter())
async def open_support_deep_link(
    message: Message,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Land a group user directly in private support intake."""
    await show_support(message, support_requests, user_model)


@router.message(Command("paysupport"))
async def open_payment_support(
    message: Message,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Keep the retired alias harmless while exposing only canonical support."""
    await show_support(message, support_requests, user_model)


@router.callback_query(SupportMenuCallback.filter())
async def navigate_support(
    query: CallbackQuery,
    callback_data: SupportMenuCallback,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Open support from privacy controls without creating a case."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /support."), show_alert=True)
        return
    cases = await support_requests.list_open(user_model.id)
    text, markup = build_support_panel(cases, source=callback_data.source)
    if isinstance(query.message, Message):
        with suppress_outbound_history():
            await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data == SUPPORT_PRIVACY_BACK_CALLBACK)
async def return_to_privacy(
    query: CallbackQuery,
    bot: Bot,
    chat_model: ChatModel | None = None,
) -> None:
    """Return from support to the privacy panel that opened it."""
    if not isinstance(query.message, Message):
        await query.answer()
        return
    from derp.handlers.context_settings import actor_can_manage, build_privacy_panel

    can_manage = await actor_can_manage(bot, query.message, query.from_user.id)
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=can_manage,
        thread_id=query.message.message_thread_id,
    )
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
    """Choose a payment when useful, then request one bounded message."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /support."), show_alert=True)
        return
    if user_model.telegram_id != query.from_user.id:
        await query.answer(_("This support button expired."), show_alert=True)
        return
    if callback_data.kind in {SupportKind.PAYMENT, SupportKind.REFUND}:
        payments = await support_requests.list_payments(user_model.id)
        text, markup = build_support_payment_panel(
            payments,
            kind=callback_data.kind,
            source=callback_data.source,
            offset=0,
        )
        if isinstance(query.message, Message):
            with suppress_outbound_history():
                await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return
    await _start_support_intake(
        query.message,
        support_requests=support_requests,
        user_model=user_model,
        kind=callback_data.kind,
        source=callback_data.source,
    )
    await query.answer()


@router.callback_query(SupportPaymentPageCallback.filter())
async def paginate_support_payments(
    query: CallbackQuery,
    callback_data: SupportPaymentPageCallback,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Page through the actor's own receipts five at a time."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /support."), show_alert=True)
        return
    payments = await support_requests.list_payments(
        user_model.id,
        offset=callback_data.offset,
    )
    text, markup = build_support_payment_panel(
        payments,
        kind=callback_data.kind,
        source=callback_data.source,
        offset=callback_data.offset,
    )
    if isinstance(query.message, Message):
        with suppress_outbound_history():
            await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(SupportPaymentCallback.filter())
async def choose_support_payment(
    query: CallbackQuery,
    callback_data: SupportPaymentCallback,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Bind an exact receipt before collecting the user's one-shot note."""
    if not _private_actor_callback(query) or user_model is None:
        await query.answer(_("Open Derp privately and use /support."), show_alert=True)
        return
    try:
        receipt_id = (
            None
            if callback_data.receipt_id == "none"
            else UUID(hex=callback_data.receipt_id)
        )
    except ValueError:
        await query.answer(_("That payment is no longer available."), show_alert=True)
        return
    try:
        await _start_support_intake(
            query.message,
            support_requests=support_requests,
            user_model=user_model,
            kind=callback_data.kind,
            source=callback_data.source,
            payment_receipt_id=receipt_id,
        )
    except SupportReceiptError:
        await query.answer(_("That payment is no longer available."), show_alert=True)
        return
    await query.answer()


@router.callback_query(SupportStatusCallback.filter())
async def refresh_support_status(
    query: CallbackQuery,
    callback_data: SupportStatusCallback,
    support_requests: SupportRequestService,
    user_model: UserModel | None = None,
) -> None:
    """Reconcile one stable receipt from durable case state."""
    if (
        not _private_actor_callback(query)
        or user_model is None
        or user_model.telegram_id != query.from_user.id
        or callback_data.actor_id != query.from_user.id
    ):
        await query.answer(_("This support button expired."), show_alert=True)
        return
    case = await support_requests.get_requester_case(
        user_model.id,
        callback_data.reference,
    )
    if (
        case is None
        or case.status_message is None
        or not isinstance(query.message, Message)
        or case.status_message.chat_id != query.message.chat.id
        or case.status_message.message_id != query.message.message_id
    ):
        await query.answer(_("This support button expired."), show_alert=True)
        return
    text, markup = build_support_status_receipt(
        case,
        actor_telegram_id=query.from_user.id,
    )
    with suppress_outbound_history():
        await query.message.edit_text(text, reply_markup=markup)
    await query.answer(_("Updated"))


@router.message(SupportReplyFilter())
async def finish_support_intake(
    message: Message,
    support_requests: SupportRequestService,
    db: DatabaseManager,
    bot: Bot,
    operator_config: OperatorControlConfig,
    support_intake_lookup: SupportIntakeLookup,
    user_model: UserModel | None = None,
) -> None:
    """Create a case from one bounded reply and turn its prompt into a receipt."""
    if not _private_actor_message(message) or user_model is None:
        return
    prompt = message.reply_to_message
    if prompt is None:
        return
    async with db.session() as session:
        await remove_disqualified_message(
            session,
            chat_telegram_id=message.chat.id,
            telegram_message_id=message.message_id,
        )
    intake = support_intake_lookup.intake
    if intake is None:
        with suppress_outbound_history():
            await message.reply(
                _("That support question expired. Open /support again.")
            )
        return
    description = (message.text or message.caption or "").strip()
    if not 1 <= len(description) <= 800:
        with suppress_outbound_history():
            await message.reply(_("Keep the message under 800 characters."))
        return
    try:
        result = await support_requests.open(
            user_model.id,
            kind=intake.kind,
            source=intake.source,
            description=description,
            payment_receipt_id=intake.payment_receipt_id,
            status_message=intake.status_message,
        )
    except SupportCapacityError:
        with suppress_outbound_history():
            await message.reply(_("You already have several open cases."))
        return
    await support_requests.discard_intake(
        user_model.id,
        prompt_chat_id=message.chat.id,
        prompt_message_id=prompt.message_id,
    )
    if result.created:
        await _notify_operators(bot, operator_config, result.case)
    elif result.status_message != intake.status_message:
        with suppress_outbound_history():
            await prompt.delete()
            await message.reply(
                _(
                    "That {kind} case is already open: <code>{reference}</code>. "
                    "I kept its original note."
                ).format(
                    kind=_support_kind(result.case.kind),
                    reference=escape(result.case.reference),
                )
            )
        return
    receipt_text, receipt_markup = build_support_status_receipt(
        result.case,
        actor_telegram_id=user_model.telegram_id,
    )
    with suppress_outbound_history():
        await prompt.edit_text(receipt_text, reply_markup=receipt_markup)


async def _start_support_intake(
    source_message: Message,
    *,
    support_requests: SupportRequestService,
    user_model: UserModel,
    kind: SupportKind,
    source: SupportSource,
    payment_receipt_id: UUID | None = None,
) -> None:
    """Send and durably bind one native ForceReply prompt."""
    with suppress_outbound_history():
        prompt = await source_message.answer(
            support_intake_prompt(),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=_("One short message"),
            ),
            protect_content=True,
        )
    try:
        await support_requests.register_intake(
            user_model.id,
            kind=kind,
            source=source,
            payment_receipt_id=payment_receipt_id,
            status_message=SupportStatusMessage(
                chat_id=prompt.chat.id,
                message_id=prompt.message_id,
            ),
        )
    except SupportCapacityError:
        with suppress_outbound_history():
            await prompt.delete()
            await source_message.answer(_("Reply to an open support question first."))


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


def _support_status(status: SupportStatus) -> str:
    return {
        SupportStatus.OPEN: _("open"),
        SupportStatus.REFUND_PENDING: _("refund pending"),
        SupportStatus.RESOLVED: _("resolved"),
        SupportStatus.DECLINED: _("declined"),
    }[status]


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
    "LegalDocumentCallback",
    "LegalCloseCallback",
    "SUPPORT_MENU_CALLBACK",
    "SupportCreateCallback",
    "TermsAcceptCallback",
    "TermsAcceptanceSource",
    "build_legal_document_page",
    "build_support_panel",
    "build_terms_panel",
    "router",
    "terms_callback_version",
]
