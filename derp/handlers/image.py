"""Thin Telegram adapters for durable image operations."""

from __future__ import annotations

from aiogram import F, Router, flags
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import gettext as _
from pydantic_ai import (
    DeferredToolRequests,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.messages import UserPromptPart

from derp.approvals import DeferredToolApprovalService
from derp.approvals.image_tools import ImageToolRunContext
from derp.catalog import GoogleModelKey, get_google_model
from derp.common.extractor import Extractor
from derp.delivery import (
    Delivered,
    DeliveryAuthorizationError,
    DeliveryFailed,
    DeliveryResendCallback,
    DeliveryService,
    DeliveryStateError,
    DeliveryUncertain,
    ProgressStage,
    ResendCallbackAuthorization,
    ResendResult,
)
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageEditRequest,
    ImageGenerateRequest,
    ImageInProgress,
    ImageNotCharged,
    ImageNotChargedReason,
    ImageOperationCoordinator,
    ImageOperationOutcome,
    ImageRefunded,
    ImageRequest,
)
from derp.filters.meta import MetaCommand, MetaInfo
from derp.handlers.tool_approvals import present_image_approvals
from derp.media import image_reference_from_telegram
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operations import ReservationRejection

router = Router(name="image")


def _funding_text(outcome: ImageAwaitingFunding) -> str:
    if outcome.reason is ReservationRejection.PERSONAL_CONSENT_REQUIRED:
        return _(
            "Funding approval is required. Not charged. Enable personal credit "
            "spending for this chat, then retry."
        )
    if outcome.reason is ReservationRejection.WALLET_IN_DEBT:
        return _("Funding is unavailable while this balance is in debt. Not charged.")
    return _("Funding needed: {credits} credits. Not charged.").format(
        credits=outcome.quote.credits
    )


def _not_charged_text(reason: ImageNotChargedReason) -> str:
    if reason is ImageNotChargedReason.INVALID_INPUT:
        return _("Not charged. The image request is invalid.")
    if reason is ImageNotChargedReason.POLICY_REJECTION:
        return _("Not charged. The image request was declined.")
    if reason is ImageNotChargedReason.UNUSABLE_OUTPUT:
        return _("Not charged. The model did not return a usable image.")
    if reason is ImageNotChargedReason.QUOTE_EXPIRED:
        return _("Not charged. The price expired; send the request again.")
    if reason is ImageNotChargedReason.CANCELED:
        return _("Not charged. The image request was canceled.")
    return _("Not charged. Image generation did not complete. Please try again.")


def _outcome_text(outcome: ImageOperationOutcome) -> str:
    if isinstance(outcome, ImageDelivered):
        return _("Image delivered.")
    if isinstance(outcome, ImageAwaitingFunding):
        return _funding_text(outcome)
    if isinstance(outcome, ImageNotCharged):
        return _not_charged_text(outcome.reason)
    if isinstance(outcome, ImageRefunded):
        return _("Refunded. Delivery failed, so the charged credits were returned.")
    if isinstance(outcome, ImageDeliveryUncertain):
        return _(
            "Delivery uncertain. The image may already have arrived. Check the chat "
            "first, then use Send again only if it is missing. Sending again does "
            "not charge credits again."
        )
    if isinstance(outcome, ImageInProgress):
        return {
            ProgressStage.PREPARING: _(
                "In progress. This image request is still being prepared."
            ),
            ProgressStage.GENERATING: _(
                "In progress. This image is still being generated."
            ),
            ProgressStage.DELIVERING: _(
                "In progress. This image is still being delivered."
            ),
        }[outcome.stage]
    raise TypeError(f"Unsupported image operation outcome: {type(outcome).__name__}")


def _outcome_markup(
    outcome: ImageOperationOutcome,
) -> InlineKeyboardMarkup | None:
    if not isinstance(outcome, ImageDeliveryUncertain):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Send again"),
                    callback_data=DeliveryResendCallback(
                        token=outcome.resend_token
                    ).pack(),
                )
            ]
        ]
    )


def _resend_outcome(
    result: ResendResult,
    *,
    resend_token: str,
) -> ImageOperationOutcome:
    outcome = result.outcome
    if isinstance(outcome, Delivered):
        return ImageDelivered(result.operation_id, outcome.message_ids)
    if isinstance(outcome, DeliveryUncertain):
        if outcome.code == "attempt_in_progress_or_interrupted":
            return ImageInProgress(
                result.operation_id,
                ProgressStage.DELIVERING,
                outcome.code,
            )
        return ImageDeliveryUncertain(
            result.operation_id,
            outcome.code,
            resend_token,
        )
    if isinstance(outcome, DeliveryFailed) and outcome.retryable:
        return ImageInProgress(
            result.operation_id,
            ProgressStage.DELIVERING,
            outcome.code,
        )
    return ImageRefunded(result.operation_id, outcome.code)


async def _edit_progress(
    progress: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Keep one status message when possible and preserve the final outcome."""
    try:
        edited = await progress.edit_text(text, reply_markup=reply_markup)
    except Exception as exc:
        report_exception(
            "image_progress_edit_failed",
            exception=exc,
            level="warning",
        )
        try:
            return await progress.reply(text, reply_markup=reply_markup)
        except Exception as fallback_exc:
            report_exception(
                "image_progress_fallback_failed",
                exception=fallback_exc,
                level="warning",
            )
            return progress
    return edited if isinstance(edited, Message) else progress


async def _finish_outcome(
    progress: Message,
    outcome: ImageOperationOutcome,
) -> Message:
    """Remove delivered progress, otherwise render the durable operation state."""
    if isinstance(outcome, ImageDelivered):
        try:
            deleted = await progress.delete()
        except Exception as exc:
            report_exception(
                "image_progress_delete_failed",
                exception=exc,
                level="warning",
            )
        else:
            if deleted:
                return progress
        return await _edit_progress(
            progress,
            _outcome_text(outcome),
        )
    return await _edit_progress(
        progress,
        _outcome_text(outcome),
        reply_markup=_outcome_markup(outcome),
    )


async def _present_command_approval(
    *,
    message: Message,
    coordinator: ImageOperationCoordinator,
    approvals: DeferredToolApprovalService,
    user_model: UserModel,
    chat_model: ChatModel,
    request: ImageRequest,
) -> Message | None:
    """Map an explicit command into the same deferred tool contract as chat."""
    if isinstance(request, ImageGenerateRequest):
        tool_call = ToolCallPart(
            "generate_image",
            {"prompt": request.prompt, "style": request.style},
            "command-image",
        )
        source = None
    else:
        tool_call = ToolCallPart(
            "edit_image",
            {"edit_prompt": request.prompt},
            "command-image",
        )
        source = request.source

    model = get_google_model(GoogleModelKey.CHAT_ECONOMY)
    history = (
        ModelRequest(parts=[UserPromptPart(request.prompt)]),
        ModelResponse(parts=[tool_call], model_name=model.provider_model_id),
    )
    context = ImageToolRunContext(
        requester_id=user_model.id,
        requester_telegram_id=user_model.telegram_id,
        chat_id=chat_model.id,
        chat_telegram_id=message.chat.id,
        message_id=message.message_id,
        thread_id=message.message_thread_id,
        business_connection_id=message.business_connection_id,
        source=source,
    )
    return await present_image_approvals(
        message=message,
        requests=DeferredToolRequests(approvals=[tool_call]),
        original_history=history,
        context=context,
        image_operations=coordinator,
        approvals=approvals,
    )


@router.callback_query(DeliveryResendCallback.filter())
async def resend_image_delivery(
    callback: CallbackQuery,
    callback_data: DeliveryResendCallback,
    delivery_service: DeliveryService,
) -> Message | None:
    """Retry one uncertain delivery within its persisted requester and scope."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer(
            _("This delivery control is unavailable."),
            show_alert=True,
        )
        return None

    await callback.answer()
    authorization = ResendCallbackAuthorization(
        token=callback_data.token,
        actor_user_id=callback.from_user.id,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
    )
    try:
        result = await delivery_service.resend_from_callback(authorization)
    except DeliveryAuthorizationError:
        return None
    except DeliveryStateError:
        return await _edit_progress(
            message,
            _("In progress. Delivery status is still being reconciled."),
        )
    except Exception as exc:
        report_exception(
            "image_resend_failed",
            exception=exc,
            telegram_chat_id=message.chat.id,
            telegram_user_id=callback.from_user.id,
        )
        return None

    outcome = _resend_outcome(result, resend_token=callback_data.token)
    return await _finish_outcome(message, outcome)


@router.callback_query(F.data.startswith("ir:"))
async def reject_malformed_image_resend(callback: CallbackQuery) -> None:
    """Answer malformed or obsolete recovery controls without doing work."""
    await callback.answer(
        _("This delivery control is invalid or expired."),
        show_alert=True,
    )


@router.message(MetaCommand("imagine", "image", "img", "и"))
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_imagine(
    message: Message,
    meta: MetaInfo,
    image_operation_coordinator: ImageOperationCoordinator,
    deferred_tool_approval_service: DeferredToolApprovalService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message | None:
    """Present the same exact image quote used by natural-language requests."""
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Usage: /imagine <prompt>"))
    if user_model is None or chat_model is None:
        return await message.reply(
            _("Could not verify your account. Please try again.")
        )

    try:
        request = ImageGenerateRequest(prompt=prompt)
    except TypeError, ValueError:
        return await message.reply(_("The image prompt is too long or invalid."))
    return await _present_command_approval(
        message=message,
        coordinator=image_operation_coordinator,
        approvals=deferred_tool_approval_service,
        user_model=user_model,
        chat_model=chat_model,
        request=request,
    )


@router.message(MetaCommand("edit", "ed", "e", "е"))
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_edit(
    message: Message,
    meta: MetaInfo,
    image_operation_coordinator: ImageOperationCoordinator,
    deferred_tool_approval_service: DeferredToolApprovalService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message | None:
    """Present a deferred edit quote while retaining only Telegram references."""
    prompt = meta.target_text
    if not prompt:
        return await message.reply(_("Reply to an image and use: /edit <prompt>"))
    if user_model is None or chat_model is None:
        return await message.reply(
            _("Could not verify your account. Please try again.")
        )

    photo = await Extractor.photo(message)
    if photo is None:
        return await message.reply(
            _("Reply to or attach an image, then use: /edit <prompt>")
        )
    try:
        request = ImageEditRequest(
            prompt=prompt,
            source=image_reference_from_telegram(photo.media),
        )
    except TypeError, ValueError:
        return await message.reply(_("That image cannot be edited."))
    return await _present_command_approval(
        message=message,
        coordinator=image_operation_coordinator,
        approvals=deferred_tool_approval_service,
        user_model=user_model,
        chat_model=chat_model,
        request=request,
    )
