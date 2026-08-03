"""Authenticated Telegram controls for deferred paid image tools."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from html import escape

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)
from aiogram.utils.i18n import gettext as _
from pydantic_ai import (
    DeferredToolRequests,
    ModelMessage,
    ModelResponse,
    ToolCallPart,
)

from derp.approvals import (
    ApprovalAuthorizationError,
    ApprovalCapability,
    ApprovalDecisionConflictError,
    ApprovalTokenCodec,
    DecisionDisposition,
    DeferredToolApprovalService,
    DeferredToolStatus,
    ResumeLease,
    ResumeUnavailable,
    ResumeUnavailableReason,
    durable_message_history,
)
from derp.approvals.image_tools import (
    EDIT_IMAGE_TOOL,
    GENERATE_IMAGE_TOOL,
    DeferredImageCall,
    DeferredImageToolError,
    ImageToolApprovalCoordinator,
    ImageToolRunContext,
    MissingImageSourceError,
    load_persisted_image_source,
)
from derp.billing import CLOSED_COMMERCE_POLICY, CommercePolicy
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseCallback, PurchaseTargetCode
from derp.catalog import (
    InferenceProvider,
    ModelSpec,
    get_google_model_by_id,
    get_openrouter_model_by_id,
)
from derp.common.extractor import Extractor
from derp.common.sender import MessageSender
from derp.config import settings
from derp.db import DatabaseManager, store_tool_transcript
from derp.delivery import DeliveryResendCallback
from derp.execution import Feature
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageGenerateRequest,
    ImageInProgress,
    ImageNotCharged,
    ImageOperationCoordinator,
    ImageOperationOutcome,
    ImageRefunded,
)
from derp.history.capture import capture_outbound_history, suppress_outbound_history
from derp.history.transcript import extract_tool_rounds, serialize_tool_rounds
from derp.media import MediaReference, image_reference_from_telegram
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operations import OperationLedger, Quote, ReservationRejection
from derp.tools.authorization import ActorRoleResolver
from derp.tools.policy import (
    ActorRole,
    ChatTool,
    ChatToolAccess,
    ChatToolPolicy,
    derive_chat_tool_access,
)

router = Router(name="tool_approvals")


class ImageApprovalAction(StrEnum):
    """Compact user decisions that fit Telegram's callback limit."""

    RUN = "r"
    CANCEL = "c"
    CHANGE_STYLE = "s"
    STYLE_AUTO = "0"
    STYLE_PHOTO = "p"
    STYLE_ILLUSTRATION = "i"
    STYLE_CINEMATIC = "f"
    USE_PERSONAL_ONCE = "m"
    ALWAYS_HERE = "a"


class ImageApprovalCallback(CallbackData, prefix="img-tool"):
    """Compact callback carrying only a decision and opaque capability."""

    action: ImageApprovalAction
    token: str


@dataclass(frozen=True, slots=True)
class ImageResumeResult:
    """Follow-up agent output paired with the actual durable image outcome."""

    text: str
    outcome: ImageOperationOutcome


_IMAGE_STYLE_VALUES: dict[ImageApprovalAction, str | None] = {
    ImageApprovalAction.STYLE_AUTO: None,
    ImageApprovalAction.STYLE_PHOTO: "photorealistic",
    ImageApprovalAction.STYLE_ILLUSTRATION: "editorial illustration",
    ImageApprovalAction.STYLE_CINEMATIC: "cinematic",
}


def approval_service(db: DatabaseManager) -> DeferredToolApprovalService:
    """Build a stateless service over the shared transaction boundary."""
    return DeferredToolApprovalService(
        db.session,
        ApprovalTokenCodec(settings.callback_signing_key),
    )


async def live_image_source(message: Message) -> MediaReference | None:
    """Capture stable metadata supported by restart-safe deferred editing."""
    photo = await Extractor.photo(message)
    if photo is None or not isinstance(photo.media, (PhotoSize, Document)):
        return None
    try:
        return image_reference_from_telegram(photo.media)
    except TypeError, ValueError:
        return None


async def present_image_approvals(
    *,
    message: Message,
    requests: DeferredToolRequests,
    original_history: Sequence[ModelMessage],
    context: ImageToolRunContext,
    image_operations: ImageOperationCoordinator,
    approvals: DeferredToolApprovalService,
    purchases_enabled: bool | None = None,
) -> Message | None:
    """Persist validated calls and render one exact, actionable quote."""
    if requests.calls or len(requests.approvals) != 1:
        with suppress_outbound_history():
            return await message.reply(
                _(
                    "I can only create one paid image at a time. You weren't charged. "
                    "Ask for one image and try again."
                )
            )
    history = durable_message_history(original_history)
    flow = ImageToolApprovalCoordinator(image_operations, approvals)
    last_message: Message | None = None
    for tool_call in requests.approvals:
        try:
            prepared = await flow.prepare(
                context=context,
                tool_call=tool_call,
                original_history=history,
            )
        except MissingImageSourceError:
            with suppress_outbound_history():
                last_message = await message.reply(
                    _(
                        "I need an image to edit. You weren't charged. "
                        "Attach one or reply to one, then try again."
                    )
                )
            continue
        except DeferredImageToolError:
            with suppress_outbound_history():
                last_message = await message.reply(
                    _(
                        "I couldn't understand that image request. You weren't charged. "
                        "Try again with one clear instruction."
                    )
                )
            continue

        editing = prepared.call.feature is Feature.IMAGE_EDIT
        purchase_callback = None
        if (
            settings.public_purchases_enabled
            if purchases_enabled is None
            else purchases_enabled
        ):
            target = (
                PurchaseTargetCode.USER
                if context.chat_telegram_id > 0
                else PurchaseTargetCode.CHAT
            )
            purchase_callback = _purchase_callback(
                prepared.quote.credits,
                target=target,
                actor_id=context.requester_telegram_id,
            )
        with suppress_outbound_history():
            last_message = await message.reply(
                _approval_text(prepared.call, prepared.quote),
                reply_markup=_decision_keyboard(
                    prepared.handle.callback_token,
                    editing=editing,
                    purchase_callback=purchase_callback,
                ),
            )
    return last_message


@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.CHANGE_STYLE)
)
async def choose_image_style(
    callback: CallbackQuery,
    callback_data: ImageApprovalCallback,
    db: DatabaseManager,
    deferred_tool_approval_service: DeferredToolApprovalService | None = None,
) -> None:
    """Open authenticated style presets without approving provider work."""
    service = deferred_tool_approval_service or approval_service(db)
    try:
        message, capability = _callback_context(callback, callback_data.token)
        snapshot = await service.inspect(capability)
    except ApprovalAuthorizationError:
        await callback.answer(
            _("I can't use this request in this chat."),
            show_alert=True,
        )
        return

    if (
        snapshot.tool_name != GENERATE_IMAGE_TOOL
        or snapshot.status is not DeferredToolStatus.PENDING
    ):
        await callback.answer(
            _("This image request is no longer editable."),
            show_alert=True,
        )
        return

    await callback.answer()
    await _edit_control(
        message,
        _("<b>Choose a style</b>\nYou'll confirm the updated price next."),
        reply_markup=_style_keyboard(callback_data.token),
    )


@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.STYLE_CINEMATIC)
)
@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.STYLE_ILLUSTRATION)
)
@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.STYLE_PHOTO)
)
@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.STYLE_AUTO)
)
async def apply_image_style(
    callback: CallbackQuery,
    callback_data: ImageApprovalCallback,
    db: DatabaseManager,
    image_operation_coordinator: ImageOperationCoordinator,
    operation_ledger: OperationLedger,
    deferred_tool_approval_service: DeferredToolApprovalService | None = None,
) -> None:
    """Replace a pending image approval with a newly quoted styled request."""
    service = deferred_tool_approval_service or approval_service(db)
    try:
        message, capability = _callback_context(callback, callback_data.token)
        decision = await service.approve(capability)
        if decision.disposition is DecisionDisposition.EXPIRED:
            await callback.answer(_("This image request has expired."), show_alert=True)
            await _edit_control(
                message,
                _("This image request expired. You weren't charged. Send it again."),
            )
            return
        claim = await service.claim_resume(capability)
    except ApprovalAuthorizationError:
        await callback.answer(
            _("I can't use this request in this chat."),
            show_alert=True,
        )
        return
    except ApprovalDecisionConflictError:
        await callback.answer(
            _("This image request is no longer editable."),
            show_alert=True,
        )
        return

    if isinstance(claim, ResumeUnavailable):
        await _answer_unavailable(callback, message, claim.reason)
        return

    old_canceled = False
    try:
        run_input = claim.build_run_input()
        old_call = _persisted_tool_call(
            run_input.message_history,
            tool_name=claim.snapshot.tool_name,
            tool_call_id=claim.snapshot.tool_call_id,
        )
        style = _IMAGE_STYLE_VALUES[callback_data.action]
        revised_call = _revised_style_call(
            old_call,
            action=callback_data.action,
            style=style,
        )
        revised_history = _replace_tool_call(
            run_input.message_history,
            old_call=old_call,
            revised_call=revised_call,
        )
        await operation_ledger.cancel(
            claim.snapshot.operation_id,
            reason="image_style_changed",
        )
        await service.cancel_resume(claim)
        old_canceled = True
        prepared = await ImageToolApprovalCoordinator(
            image_operation_coordinator,
            service,
        ).prepare(
            context=ImageToolRunContext(
                requester_id=claim.snapshot.requester_id,
                requester_telegram_id=claim.snapshot.requester_telegram_id,
                chat_id=claim.snapshot.chat_id,
                chat_telegram_id=claim.snapshot.chat_telegram_id,
                message_id=claim.snapshot.message_id,
                thread_id=claim.snapshot.thread_id,
                business_connection_id=message.business_connection_id,
            ),
            tool_call=revised_call,
            original_history=revised_history,
        )
    except Exception as exc:
        if not old_canceled:
            await service.release_resume(claim)
        report_exception(
            "image_style_change_failed",
            exception=exc,
            request_id=str(claim.snapshot.request_id),
            operation_id=str(claim.snapshot.operation_id),
        )
        await callback.answer()
        await _edit_control(
            message,
            _(
                "I couldn't change the style. You weren't charged. Send the request again."
            ),
        )
        return

    purchase_callback = None
    if settings.public_purchases_enabled:
        purchase_callback = _purchase_callback(
            prepared.quote.credits,
            target=(
                PurchaseTargetCode.USER
                if claim.snapshot.chat_telegram_id > 0
                else PurchaseTargetCode.CHAT
            ),
            actor_id=claim.snapshot.requester_telegram_id,
        )
    await callback.answer(_("Style updated"))
    await _edit_control(
        message,
        _approval_text(prepared.call, prepared.quote),
        reply_markup=_decision_keyboard(
            prepared.handle.callback_token,
            editing=False,
            purchase_callback=purchase_callback,
        ),
    )


@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.CANCEL)
)
async def deny_image_tool(
    callback: CallbackQuery,
    callback_data: ImageApprovalCallback,
    db: DatabaseManager,
    deferred_tool_approval_service: DeferredToolApprovalService | None = None,
) -> None:
    """Apply an authenticated cancellation without reserving or charging funds."""
    service = deferred_tool_approval_service or approval_service(db)
    try:
        message, capability = _callback_context(callback, callback_data.token)
        decision = await service.deny(capability)
    except ApprovalAuthorizationError:
        await callback.answer(
            _("I can't use this request in this chat."),
            show_alert=True,
        )
        return
    except ApprovalDecisionConflictError:
        await callback.answer(
            _("This image request is already running."), show_alert=True
        )
        return

    await callback.answer()
    text = (
        _("This image request expired. You weren't charged. Send it again.")
        if decision.disposition is DecisionDisposition.EXPIRED
        else _("Canceled. You weren't charged.")
    )
    await _edit_control(message, text)


@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.USE_PERSONAL_ONCE)
)
@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.ALWAYS_HERE)
)
@router.callback_query(
    ImageApprovalCallback.filter(F.action == ImageApprovalAction.RUN)
)
async def approve_image_tool(
    callback: CallbackQuery,
    callback_data: ImageApprovalCallback,
    db: DatabaseManager,
    image_operation_coordinator: ImageOperationCoordinator,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
    actor_role_resolver: ActorRoleResolver | None = None,
    deferred_tool_approval_service: DeferredToolApprovalService | None = None,
    operation_ledger: OperationLedger | None = None,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
) -> None:
    """Resume only server-persisted calls authorized for this actor and topic."""
    service = deferred_tool_approval_service or approval_service(db)
    personal_consent: tuple[OperationLedger, uuid.UUID, uuid.UUID] | None = None
    try:
        message, capability = _callback_context(callback, callback_data.token)
        decision = await service.approve(capability)
        if decision.disposition is DecisionDisposition.EXPIRED:
            await callback.answer(_("This image request has expired."), show_alert=True)
            await _edit_control(
                message,
                _("This image request expired. You weren't charged. Send it again."),
            )
            return
        if callback_data.action is ImageApprovalAction.ALWAYS_HERE:
            if (
                operation_ledger is None
                or user_model is None
                or chat_model is None
                or user_model.id != decision.snapshot.requester_id
                or user_model.telegram_id != decision.snapshot.requester_telegram_id
                or chat_model.id != decision.snapshot.chat_id
                or chat_model.telegram_id != decision.snapshot.chat_telegram_id
            ):
                raise ApprovalAuthorizationError(
                    "personal consent models do not match the approval scope"
                )
            personal_consent = (operation_ledger, user_model.id, chat_model.id)
        claim = await service.claim_resume(capability)
    except ApprovalAuthorizationError:
        await callback.answer(
            _("I can't use this request in this chat."),
            show_alert=True,
        )
        return
    except ApprovalDecisionConflictError:
        await callback.answer(_("This image request was canceled."), show_alert=True)
        return

    if isinstance(claim, ResumeUnavailable):
        await _answer_unavailable(callback, message, claim.reason)
        return

    await callback.answer(_("Started"))
    await _edit_control(message, _("Creating your image..."))
    try:
        if personal_consent is not None:
            ledger, user_id, chat_id = personal_consent
            await ledger.grant_personal_consent(user_id, chat_id)
        resumed = await _resume_approved_image(
            callback=callback,
            message=message,
            lease=claim,
            db=db,
            image_operations=image_operation_coordinator,
            user_model=user_model,
            chat_model=chat_model,
            actor_role_resolver=actor_role_resolver,
            allow_personal_once=(
                callback_data.action is ImageApprovalAction.USE_PERSONAL_ONCE
            ),
        )
        if isinstance(resumed.outcome, ImageAwaitingFunding):
            await service.release_resume(claim)
            await _edit_control(
                message,
                _funding_text(resumed.outcome),
                reply_markup=_funding_keyboard(
                    callback_data.token,
                    personal_once=(
                        resumed.outcome.reason
                        is ReservationRejection.PERSONAL_CONSENT_REQUIRED
                    ),
                    purchase_callback=(
                        _chat_purchase_callback(resumed.outcome.quote.credits)
                        if commerce_policy.public_intake_enabled
                        and chat_model is not None
                        and chat_model.type != "private"
                        else None
                    ),
                ),
            )
            return
        await service.mark_resumed(claim)
    except Exception as exc:
        await service.release_resume(claim)
        report_exception(
            "deferred_image_resume_failed",
            exception=exc,
            request_id=str(claim.snapshot.request_id),
            operation_id=str(claim.snapshot.operation_id),
        )
        await _edit_control(
            message,
            _("I couldn't finish the image. Tap Try again."),
            reply_markup=_retry_keyboard(callback_data.token),
        )
        return

    await _render_image_outcome(message, resumed.outcome)
    if resumed.text:
        sender = MessageSender(
            bot=callback.bot,
            chat_id=claim.snapshot.chat_telegram_id,
            thread_id=claim.snapshot.thread_id,
            business_connection_id=message.business_connection_id,
        )
        try:
            with capture_outbound_history():
                await sender.send(resumed.text)
        except Exception as exc:
            report_exception(
                "deferred_image_followup_send_failed",
                exception=exc,
                level="warning",
                request_id=str(claim.snapshot.request_id),
            )


async def _resume_approved_image(
    *,
    callback: CallbackQuery,
    message: Message,
    lease: ResumeLease,
    db: DatabaseManager,
    image_operations: ImageOperationCoordinator,
    user_model: UserModel | None,
    chat_model: ChatModel | None,
    actor_role_resolver: ActorRoleResolver | None,
    allow_personal_once: bool,
) -> ImageResumeResult:
    if user_model is None or user_model.id != lease.snapshot.requester_id:
        raise RuntimeError("approved image requester is unavailable")
    if chat_model is None or chat_model.id != lease.snapshot.chat_id:
        raise RuntimeError("approved image chat is unavailable")

    run_input = lease.build_run_input()
    tool_access = await _current_tool_access(
        callback=callback,
        message=message,
        chat_model=chat_model,
        actor_role_resolver=actor_role_resolver,
    )
    required_tool = (
        ChatTool.EDIT_IMAGE
        if lease.snapshot.tool_name == EDIT_IMAGE_TOOL
        else ChatTool.GENERATE_IMAGE
    )
    if not tool_access.allows(required_tool):
        raise RuntimeError("image tools are no longer enabled for this chat")

    source = await load_persisted_image_source(
        db,
        chat_id=lease.snapshot.chat_id,
        message_id=lease.snapshot.message_id,
    )
    image_context = ImageToolRunContext(
        requester_id=lease.snapshot.requester_id,
        requester_telegram_id=lease.snapshot.requester_telegram_id,
        chat_id=lease.snapshot.chat_id,
        chat_telegram_id=lease.snapshot.chat_telegram_id,
        message_id=lease.snapshot.message_id,
        thread_id=lease.snapshot.thread_id,
        business_connection_id=message.business_connection_id,
        source=source,
        allow_personal_once=allow_personal_once,
    )
    tool_call = _persisted_tool_call(
        run_input.message_history,
        tool_name=lease.snapshot.tool_name,
        tool_call_id=lease.snapshot.tool_call_id,
    )
    deferred_call = DeferredImageCall.parse(tool_call, source=source)
    image_plan = await image_operations.execution_plan_for_quote(
        lease.snapshot.operation_id,
        deferred_call.feature,
    )
    outcome = await image_operations.run(
        deferred_call.invocation(image_context),
        image_plan,
        deferred_call.request,
        allow_personal_once=allow_personal_once,
    )
    if isinstance(outcome, ImageAwaitingFunding):
        return ImageResumeResult("", outcome)
    if not isinstance(
        outcome,
        (
            ImageAwaitingFunding,
            ImageDelivered,
            ImageDeliveryUncertain,
            ImageInProgress,
            ImageNotCharged,
            ImageRefunded,
        ),
    ):
        raise RuntimeError("approved image tool produced no durable outcome")
    return ImageResumeResult("", outcome)


def _persisted_tool_call(
    history: Sequence[ModelMessage],
    *,
    tool_name: str,
    tool_call_id: str,
) -> ToolCallPart:
    if not history or not isinstance(history[-1], ModelResponse):
        raise RuntimeError("deferred image history has no final model response")
    calls = [
        part
        for part in history[-1].parts
        if isinstance(part, ToolCallPart)
        and part.tool_name == tool_name
        and part.tool_call_id == tool_call_id
    ]
    if len(calls) != 1:
        raise RuntimeError("deferred image tool identity is missing or ambiguous")
    return calls[0]


async def _current_tool_access(
    *,
    callback: CallbackQuery,
    message: Message,
    chat_model: ChatModel,
    actor_role_resolver: ActorRoleResolver | None,
) -> ChatToolAccess:
    if actor_role_resolver is not None and callback.from_user is not None:
        role = await actor_role_resolver.resolve(
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            user_id=callback.from_user.id,
        )
    elif message.chat.type == "private":
        role = ActorRole.PRIVATE_OWNER
    else:
        role = ActorRole.MEMBER
    return derive_chat_tool_access(role, ChatToolPolicy.from_chat(chat_model))


async def _store_resumed_transcript(
    db: DatabaseManager,
    *,
    chat_telegram_id: int,
    message_id: int,
    history: Sequence[ModelMessage],
) -> None:
    rounds = extract_tool_rounds(history)
    if not rounds:
        return
    async with db.session() as session:
        await store_tool_transcript(
            session,
            chat_telegram_id=chat_telegram_id,
            telegram_message_id=message_id,
            tool_rounds=serialize_tool_rounds(rounds),
        )


def _callback_context(
    callback: CallbackQuery,
    token: str,
) -> tuple[Message, ApprovalCapability]:
    if callback.from_user is None or not isinstance(callback.message, Message):
        raise ApprovalAuthorizationError("approval callback has no message context")
    message = callback.message
    return message, ApprovalCapability(
        token=token,
        requester_telegram_id=callback.from_user.id,
        chat_telegram_id=message.chat.id,
        thread_id=message.message_thread_id,
    )


def _approval_text(call: DeferredImageCall, quote: Quote) -> str:
    """Render fixed commercial and privacy facts without repeating the prompt."""
    model = _model_for_quote(quote)
    credit_text = _(
        "{count} credit",
        "{count} credits",
        quote.credits,
    ).format(count=quote.credits)
    headline = (
        _("Edit image?") if call.feature is Feature.IMAGE_EDIT else _("Generate image?")
    )
    lines = [
        f"<b>{headline}</b>",
        _("{credits} · {model}").format(
            credits=credit_text,
            model=escape(model.display_name),
        ),
        (
            _("Private · zero-data retention")
            if model.routing is not None and model.routing.zero_data_retention
            else _("Provider retention policy applies")
        ),
        _("<b>Prompt:</b> {prompt}").format(
            prompt=escape(_prompt_preview(call.request.prompt))
        ),
    ]
    if isinstance(call.request, ImageGenerateRequest):
        style = escape(call.request.style) if call.request.style else _("Automatic")
        lines.append(_("Style: {style}").format(style=style))
    return "\n".join(lines)


def _prompt_preview(prompt: str, *, limit: int = 280) -> str:
    """Keep approvals inspectable without approaching Telegram's text limit."""
    compact = " ".join(prompt.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _model_for_quote(quote: Quote) -> ModelSpec:
    resolver = (
        get_openrouter_model_by_id
        if quote.provider is InferenceProvider.OPENROUTER
        else get_google_model_by_id
    )
    return resolver(quote.provider_model_id)


def _decision_keyboard(
    token: str,
    *,
    editing: bool,
    purchase_callback: str | None,
) -> InlineKeyboardMarkup:
    primary = InlineKeyboardButton(
        text=_("Apply edit") if editing else _("Generate"),
        callback_data=_pack_callback(ImageApprovalAction.RUN, token),
    )
    first_row = [primary]
    if not editing:
        first_row.append(
            InlineKeyboardButton(
                text=_("Change style"),
                callback_data=_pack_callback(ImageApprovalAction.CHANGE_STYLE, token),
            )
        )
    final_row: list[InlineKeyboardButton] = []
    if purchase_callback is not None:
        final_row.append(
            InlineKeyboardButton(
                text=_("Buy credits"),
                callback_data=purchase_callback,
            )
        )
    final_row.append(
        InlineKeyboardButton(
            text=_("Cancel"),
            callback_data=_pack_callback(ImageApprovalAction.CANCEL, token),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[first_row, final_row])


def _style_keyboard(token: str) -> InlineKeyboardMarkup:
    options = (
        (ImageApprovalAction.STYLE_AUTO, _("Automatic")),
        (ImageApprovalAction.STYLE_PHOTO, _("Photo")),
        (ImageApprovalAction.STYLE_ILLUSTRATION, _("Illustration")),
        (ImageApprovalAction.STYLE_CINEMATIC, _("Cinematic")),
    )
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=_pack_callback(action, token),
            )
            for action, label in options[offset : offset + 2]
        ]
        for offset in range(0, len(options), 2)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Cancel"),
                callback_data=_pack_callback(ImageApprovalAction.CANCEL, token),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _revised_style_call(
    tool_call: ToolCallPart,
    *,
    action: ImageApprovalAction,
    style: str | None,
) -> ToolCallPart:
    if tool_call.tool_name != GENERATE_IMAGE_TOOL:
        raise ValueError("only image generation has a style")
    arguments = tool_call.args_as_dict(raise_if_invalid=True)
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("image generation prompt is invalid")
    digest = hashlib.sha256(
        f"{tool_call.tool_call_id}:{action.value}".encode()
    ).hexdigest()[:24]
    return ToolCallPart(
        GENERATE_IMAGE_TOOL,
        {"prompt": prompt, "style": style},
        f"style-{digest}",
    )


def _replace_tool_call(
    history: Sequence[ModelMessage],
    *,
    old_call: ToolCallPart,
    revised_call: ToolCallPart,
) -> tuple[ModelMessage, ...]:
    replacements = 0
    revised_history: list[ModelMessage] = []
    for message in history:
        if not isinstance(message, ModelResponse):
            revised_history.append(message)
            continue
        parts = []
        for part in message.parts:
            if (
                isinstance(part, ToolCallPart)
                and part.tool_name == old_call.tool_name
                and part.tool_call_id == old_call.tool_call_id
            ):
                parts.append(revised_call)
                replacements += 1
            else:
                parts.append(part)
        revised_history.append(replace(message, parts=parts))
    if replacements != 1:
        raise ValueError("image approval history has no unique tool call")
    return tuple(revised_history)


def _retry_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Try again"),
                    callback_data=_pack_callback(ImageApprovalAction.RUN, token),
                )
            ]
        ]
    )


def _funding_keyboard(
    token: str,
    *,
    personal_once: bool,
    purchase_callback: str | None,
) -> InlineKeyboardMarkup:
    retry = InlineKeyboardButton(
        text=_("Try again"),
        callback_data=_pack_callback(ImageApprovalAction.RUN, token),
    )
    personal_buttons: list[InlineKeyboardButton] = []
    if personal_once:
        personal_buttons = [
            InlineKeyboardButton(
                text=_("Use my credits once"),
                callback_data=_pack_callback(
                    ImageApprovalAction.USE_PERSONAL_ONCE,
                    token,
                ),
            ),
            InlineKeyboardButton(
                text=_("Always use my credits"),
                callback_data=_pack_callback(
                    ImageApprovalAction.ALWAYS_HERE,
                    token,
                ),
            ),
        ]
    funding_buttons = [
        InlineKeyboardButton(
            text=_("Buy chat credits"),
            callback_data=purchase_callback,
        )
        if purchase_callback is not None
        else retry
    ]
    if purchase_callback is not None:
        funding_buttons.append(retry)
    rows = [
        row
        for row in (
            personal_buttons,
            funding_buttons,
        )
        if row
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _purchase_callback(
    required_credits: int,
    *,
    target: PurchaseTargetCode,
    actor_id: int,
) -> str | None:
    eligible = (
        product
        for product in DEFAULT_PRODUCT_CATALOG.current_top_ups.values()
        if product.credits >= required_credits
    )
    product = min(eligible, key=lambda item: (item.credits, item.stars), default=None)
    if product is None:
        return None
    packed = PurchaseCallback(
        kind=product.kind,
        product_id=product.id,
        target=target,
        actor_id=actor_id,
    ).pack()
    if len(packed.encode("utf-8")) > 64:
        raise ValueError("purchase callback exceeds Telegram's 64-byte limit")
    return packed


def _chat_purchase_callback(required_credits: int) -> str | None:
    """Retain the funding-panel compatibility path for shared wallets."""
    return _purchase_callback(
        required_credits,
        target=PurchaseTargetCode.CHAT,
        actor_id=0,
    )


def _pack_callback(action: ImageApprovalAction, token: str) -> str:
    packed = ImageApprovalCallback(action=action, token=token).pack()
    if len(packed.encode("utf-8")) > 64:
        raise ValueError("image approval callback exceeds Telegram's 64-byte limit")
    return packed


def _funding_text(outcome: ImageAwaitingFunding) -> str:
    if outcome.reason is ReservationRejection.PERSONAL_CONSENT_REQUIRED:
        return _(
            "This chat can't cover the image. You weren't charged. "
            "Use your credits once or add chat credits."
        )
    if outcome.reason is ReservationRejection.WALLET_IN_DEBT:
        return _(
            "Paid features are paused for this balance. You weren't charged. "
            "Clear the payment debt to continue."
        )
    credit_count = outcome.quote.credits
    return _(
        "This image needs {credits} credit. You weren't charged. "
        "Add credits and try again.",
        "This image needs {credits} credits. You weren't charged. "
        "Add credits and try again.",
        credit_count,
    ).format(credits=credit_count)


async def _render_image_outcome(
    message: Message,
    outcome: ImageOperationOutcome,
) -> None:
    if isinstance(outcome, ImageDelivered):
        await _delete_control(message)
        return
    if isinstance(outcome, ImageDeliveryUncertain):
        callback_data = DeliveryResendCallback(token=outcome.resend_token).pack()
        if len(callback_data.encode("utf-8")) > 64:
            raise ValueError("image resend callback exceeds Telegram's 64-byte limit")
        await _edit_control(
            message,
            _(
                "The image may already be in the chat. You won't be charged again. "
                "Check first, then tap Send again if it's missing."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_("Send again"),
                            callback_data=callback_data,
                        )
                    ]
                ]
            ),
        )
        return
    if isinstance(outcome, ImageRefunded):
        await _edit_control(
            message,
            _("I couldn't deliver the image. Your credits were returned."),
        )
        return
    if isinstance(outcome, ImageNotCharged):
        await _edit_control(
            message,
            _("I couldn't create the image. You weren't charged. Try again."),
        )
        return
    if isinstance(outcome, ImageInProgress):
        await _edit_control(message, _("Still working on your image..."))
        return
    if isinstance(outcome, ImageAwaitingFunding):
        raise RuntimeError("funding outcome must release its approval lease")
    raise TypeError(f"unsupported image outcome: {type(outcome).__name__}")


async def _delete_control(message: Message) -> None:
    try:
        with suppress_outbound_history():
            await message.delete()
    except Exception as exc:
        report_exception(
            "image_approval_control_delete_failed",
            exception=exc,
            level="warning",
        )


async def _answer_unavailable(
    callback: CallbackQuery,
    message: Message,
    reason: ResumeUnavailableReason,
) -> None:
    if reason is ResumeUnavailableReason.LEASED:
        await callback.answer(
            _("This image request is already running."), show_alert=True
        )
        return
    if reason is ResumeUnavailableReason.RESUMED:
        await callback.answer(
            _("This image request is already complete."), show_alert=True
        )
        await _edit_control(message, _("Image request complete."))
        return
    await callback.answer(_("This image request has expired."), show_alert=True)
    await _edit_control(
        message,
        _("This image request expired. You weren't charged. Send it again."),
    )


async def _edit_control(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        with suppress_outbound_history():
            await message.edit_text(text, reply_markup=reply_markup)
    except Exception as exc:
        report_exception(
            "image_approval_control_edit_failed",
            exception=exc,
            level="warning",
        )


__all__ = [
    "ImageApprovalAction",
    "ImageApprovalCallback",
    "ImageResumeResult",
    "approval_service",
    "apply_image_style",
    "approve_image_tool",
    "choose_image_style",
    "deny_image_tool",
    "live_image_source",
    "present_image_approvals",
    "router",
]
