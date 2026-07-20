"""Thin Telegram adapters for durable image operations."""

from __future__ import annotations

from aiogram import Router, flags
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.catalog import GoogleModelKey
from derp.common.extractor import Extractor
from derp.delivery import DeliveryTarget, ProgressStage
from derp.execution import Feature, plan_execution
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageEditRequest,
    ImageGenerateRequest,
    ImageInProgress,
    ImageInvocation,
    ImageNotCharged,
    ImageNotChargedReason,
    ImageOperationCoordinator,
    ImageOperationOutcome,
    ImageRefunded,
    ImageRequest,
)
from derp.filters.meta import MetaCommand, MetaInfo
from derp.history.core import DEFAULT_TOKEN_ESTIMATOR
from derp.media import image_reference_from_telegram
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operations import OperationId, ReservationRejection

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
            "Delivery uncertain. The image may already have arrived. Do not retry yet."
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


async def _edit_progress(progress: Message, text: str) -> Message:
    """Keep one status message when possible and preserve the final outcome."""
    try:
        edited = await progress.edit_text(text)
    except Exception as exc:
        report_exception(
            "image_progress_edit_failed",
            exception=exc,
            level="warning",
        )
        try:
            return await progress.reply(text)
        except Exception as fallback_exc:
            report_exception(
                "image_progress_fallback_failed",
                exception=fallback_exc,
                level="warning",
            )
            return progress
    return edited if isinstance(edited, Message) else progress


def _delivery_target(message: Message, reply_to: Message) -> DeliveryTarget:
    return DeliveryTarget(
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
        reply_to_message_id=reply_to.message_id,
        business_connection_id=message.business_connection_id,
    )


async def _run_image_operation(
    *,
    message: Message,
    meta: MetaInfo,
    coordinator: ImageOperationCoordinator,
    user_model: UserModel,
    chat_model: ChatModel,
    feature: Feature,
    request: ImageRequest,
) -> Message:
    operation_id = OperationId.for_command(
        feature=feature,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )
    target_message = meta.target_message
    target = _delivery_target(message, target_message)
    invocation = ImageInvocation(
        operation_id=operation_id,
        request_key=(
            f"telegram:command:{feature.value}:{message.chat.id}:{message.message_id}"
        ),
        requester_id=user_model.id,
        chat_id=chat_model.id,
        thread_id=target.thread_id,
        target=target,
        input_tokens=DEFAULT_TOKEN_ESTIMATOR.estimate_text(request.prompt),
    )
    plan = plan_execution(feature, GoogleModelKey.IMAGE)

    progress = await message.reply(_("Preparing image..."))
    progress = await _edit_progress(progress, _("Generating image..."))
    try:
        outcome = await coordinator.run(invocation, plan, request)
    except Exception as exc:
        report_exception(
            "image_operation_failed",
            exception=exc,
            operation_id=str(operation_id),
            feature=feature.value,
        )
        return await _edit_progress(
            progress,
            _(
                "The image request could not be completed. "
                "Check your credit balance before retrying."
            ),
        )
    return await _edit_progress(progress, _outcome_text(outcome))


@router.message(MetaCommand("imagine", "image", "img", "и"))
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_imagine(
    message: Message,
    meta: MetaInfo,
    image_operation_coordinator: ImageOperationCoordinator,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Create one durable image-generation operation from a Telegram command."""
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
    return await _run_image_operation(
        message=message,
        meta=meta,
        coordinator=image_operation_coordinator,
        user_model=user_model,
        chat_model=chat_model,
        feature=Feature.IMAGE_GENERATE,
        request=request,
    )


@router.message(MetaCommand("edit", "ed", "e", "е"))
@flags.chat_action(initial_sleep=2, action="upload_photo")
async def handle_edit(
    message: Message,
    meta: MetaInfo,
    image_operation_coordinator: ImageOperationCoordinator,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    """Create one durable image-edit operation from Telegram source metadata."""
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
    return await _run_image_operation(
        message=message,
        meta=meta,
        coordinator=image_operation_coordinator,
        user_model=user_model,
        chat_model=chat_model,
        feature=Feature.IMAGE_EDIT,
        request=request,
    )
