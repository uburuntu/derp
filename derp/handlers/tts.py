"""Thin Telegram adapters for approved durable text-to-speech operations."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.i18n import gettext as _

from derp.approvals import (
    ApprovalAuthorizationError,
    ApprovalDecisionConflictError,
    DecisionDisposition,
    ResumeLease,
    ResumeUnavailable,
    ResumeUnavailableReason,
)
from derp.approvals.paid_media import (
    PaidMediaApprovalAction,
    PaidMediaApprovalCallback,
    PaidMediaApprovalCoordinator,
    PaidMediaApprovalError,
    PaidMediaApprovalKind,
    PaidMediaRunContext,
    approval_callback_context,
)
from derp.billing import CLOSED_COMMERCE_POLICY, CommercePolicy
from derp.billing.telegram import PurchaseTargetCode
from derp.features import MAX_TTS_OUTPUT_SECONDS, TtsRequest
from derp.features.paid_media_operation import (
    PaidMediaAwaitingFunding,
    PaidMediaDelivered,
    PaidMediaDeliveryUncertain,
    PaidMediaInProgress,
    PaidMediaNotCharged,
    PaidMediaNotChargedReason,
    PaidMediaOperationOutcome,
    PaidMediaRefunded,
)
from derp.features.tts_operation import (
    TtsPaidMediaAdapter,
    tts_command_history,
    tts_command_tool_call,
)
from derp.filters.meta import MetaCommand, MetaInfo
from derp.handlers.paid_media_controls import (
    paid_media_decision_keyboard,
    paid_media_funding_keyboard,
    paid_media_purchase_control,
    paid_media_retry_keyboard,
)
from derp.handlers.paid_media_delivery import paid_media_resend_markup
from derp.history.capture import suppress_outbound_history
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operations import ReservationRejection

router = Router(name="tts")


@router.message(MetaCommand("tts", "voice", "say"))
async def handle_tts(
    message: Message,
    meta: MetaInfo,
    paid_media_approval_coordinator: PaidMediaApprovalCoordinator,
    tts_paid_media_adapter: TtsPaidMediaAdapter,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message | None:
    """Quote and persist one command without calling the TTS provider."""
    text = meta.target_text
    if not text:
        return await message.reply(_("Usage: /tts <text>"))
    if user_model is None or chat_model is None:
        return await message.reply(
            _("Could not verify your account. Please try again. Not charged.")
        )
    try:
        request = TtsRequest(text, MAX_TTS_OUTPUT_SECONDS)
    except TypeError, ValueError:
        return await message.reply(
            _("The voice text is too long or invalid. Not charged.")
        )

    tool_call = tts_command_tool_call(request)
    context = PaidMediaRunContext(
        requester_id=user_model.id,
        requester_telegram_id=user_model.telegram_id,
        chat_id=chat_model.id,
        chat_telegram_id=message.chat.id,
        message_id=message.message_id,
        thread_id=message.message_thread_id,
        business_connection_id=message.business_connection_id,
    )
    try:
        prepared = await paid_media_approval_coordinator.prepare(
            context=context,
            tool_call=tool_call,
            original_history=tts_command_history(request, tool_call),
            adapter=tts_paid_media_adapter,
        )
    except Exception as exc:
        report_exception(
            "tts_approval_prepare_failed",
            exception=exc,
            level="warning",
            telegram_chat_id=message.chat.id,
            telegram_user_id=user_model.telegram_id,
        )
        return await message.reply(
            _("Could not prepare this voice request. Please try again. Not charged.")
        )

    with suppress_outbound_history():
        return await message.reply(
            _("Voice generation costs {credits} credits. Run it?").format(
                credits=prepared.quote.credits
            ),
            reply_markup=paid_media_decision_keyboard(
                PaidMediaApprovalKind.TTS,
                prepared.handle.callback_token,
            ),
        )


@router.callback_query(
    PaidMediaApprovalCallback.filter(
        (F.kind == PaidMediaApprovalKind.TTS)
        & (F.action == PaidMediaApprovalAction.CANCEL)
    )
)
async def deny_tts_approval(
    callback: CallbackQuery,
    callback_data: PaidMediaApprovalCallback,
    paid_media_approval_coordinator: PaidMediaApprovalCoordinator,
) -> None:
    """Cancel the authenticated quote without reserving or charging credits."""
    try:
        message, capability = approval_callback_context(
            callback,
            callback_data.token,
        )
        decision = await paid_media_approval_coordinator.cancel(capability)
    except ApprovalAuthorizationError:
        await callback.answer(
            _("This approval is not valid in this chat."),
            show_alert=True,
        )
        return
    except ApprovalDecisionConflictError:
        await callback.answer(
            _("This voice request is already running."),
            show_alert=True,
        )
        return
    except Exception as exc:
        report_exception(
            "tts_approval_cancel_failed",
            exception=exc,
            telegram_user_id=callback.from_user.id,
        )
        await callback.answer(_("Could not cancel this request."), show_alert=True)
        return

    await callback.answer()
    await _edit_control(
        message,
        _("This voice approval expired. Not charged.")
        if decision.disposition is DecisionDisposition.EXPIRED
        else _("Canceled. Not charged."),
    )


@router.callback_query(
    PaidMediaApprovalCallback.filter(
        (F.kind == PaidMediaApprovalKind.TTS)
        & (F.action == PaidMediaApprovalAction.USE_PERSONAL_ONCE)
    )
)
@router.callback_query(
    PaidMediaApprovalCallback.filter(
        (F.kind == PaidMediaApprovalKind.TTS)
        & (F.action == PaidMediaApprovalAction.ALWAYS_HERE)
    )
)
@router.callback_query(
    PaidMediaApprovalCallback.filter(
        (F.kind == PaidMediaApprovalKind.TTS)
        & (F.action == PaidMediaApprovalAction.RUN)
    )
)
async def run_tts_approval(
    callback: CallbackQuery,
    callback_data: PaidMediaApprovalCallback,
    paid_media_approval_coordinator: PaidMediaApprovalCoordinator,
    tts_paid_media_adapter: TtsPaidMediaAdapter,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
    commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
) -> None:
    """Run only the persisted request authorized for this actor and topic."""
    try:
        message, capability = approval_callback_context(
            callback,
            callback_data.token,
        )
        approval = await paid_media_approval_coordinator.approve_and_claim(capability)
    except ApprovalAuthorizationError:
        await callback.answer(
            _("This approval is not valid in this chat."),
            show_alert=True,
        )
        return
    except ApprovalDecisionConflictError:
        await callback.answer(
            _("This voice request was canceled."),
            show_alert=True,
        )
        return
    except Exception as exc:
        report_exception(
            "tts_approval_claim_failed",
            exception=exc,
            telegram_user_id=callback.from_user.id,
        )
        await callback.answer(
            _("Could not start this voice request."),
            show_alert=True,
        )
        return

    if approval.decision.disposition is DecisionDisposition.EXPIRED:
        await callback.answer(_("This voice approval expired."), show_alert=True)
        try:
            outcome = await paid_media_approval_coordinator.reconcile_expired(
                approval.decision.snapshot
            )
        except Exception as exc:
            report_exception(
                "tts_expired_approval_reconciliation_failed",
                exception=exc,
                request_id=str(approval.decision.snapshot.request_id),
                operation_id=str(approval.decision.snapshot.operation_id),
            )
            await _edit_control(
                message,
                _("This voice approval expired. Delivery status is being checked."),
            )
            return
        await _render_tts_outcome(message, outcome)
        return
    claim = approval.claim
    if isinstance(claim, ResumeUnavailable):
        await _answer_unavailable(callback, message, claim.reason)
        return
    if not isinstance(claim, ResumeLease):
        raise RuntimeError("live voice approval did not return a resume lease")

    await callback.answer(_("Started"))
    try:
        if callback_data.action is PaidMediaApprovalAction.ALWAYS_HERE:
            if user_model is None or chat_model is None:
                raise PaidMediaApprovalError(
                    "personal consent requires persisted user and chat models"
                )
            await paid_media_approval_coordinator.grant_personal_consent(
                claim.snapshot,
                requester_id=user_model.id,
                chat_id=chat_model.id,
            )

        context = PaidMediaRunContext.from_snapshot(
            claim.snapshot,
            business_connection_id=message.business_connection_id,
        )
        recovery_markup = paid_media_retry_keyboard(
            PaidMediaApprovalKind.TTS,
            callback_data.token,
        )

        async def progress(stage) -> None:
            await _edit_control(
                message,
                {
                    "preparing": _("Preparing voice..."),
                    "generating": _("Generating voice..."),
                    "delivering": _("Delivering voice..."),
                }[stage.value],
                reply_markup=recovery_markup,
            )

        resumed = await paid_media_approval_coordinator.resume(
            lease=claim,
            context=context,
            adapter=tts_paid_media_adapter,
            allow_personal_once=(
                callback_data.action is PaidMediaApprovalAction.USE_PERSONAL_ONCE
            ),
            progress=progress,
        )
        claim = resumed.lease
        outcome = resumed.outcome
        if isinstance(outcome, PaidMediaAwaitingFunding):
            await paid_media_approval_coordinator.release(claim)
            await _edit_control(
                message,
                _funding_text(outcome),
                reply_markup=paid_media_funding_keyboard(
                    PaidMediaApprovalKind.TTS,
                    callback_data.token,
                    personal_once=(
                        outcome.reason is ReservationRejection.PERSONAL_CONSENT_REQUIRED
                    ),
                    purchase=(
                        paid_media_purchase_control(
                            outcome.quote.credits,
                            _purchase_target(chat_model),
                        )
                        if commerce_policy.public_intake_enabled
                        and chat_model is not None
                        else None
                    ),
                ),
            )
            return
        if isinstance(outcome, PaidMediaInProgress):
            await paid_media_approval_coordinator.release(claim)
            await _render_tts_outcome(
                message,
                outcome,
                retry_markup=paid_media_retry_keyboard(
                    PaidMediaApprovalKind.TTS,
                    callback_data.token,
                ),
            )
            return
        await paid_media_approval_coordinator.complete(claim)
    except Exception as exc:
        await paid_media_approval_coordinator.release(claim)
        report_exception(
            "tts_approval_resume_failed",
            exception=exc,
            request_id=str(claim.snapshot.request_id),
            operation_id=str(claim.snapshot.operation_id),
        )
        await _edit_control(
            message,
            _("Could not finish this voice request. Tap Run to retry."),
            reply_markup=paid_media_retry_keyboard(
                PaidMediaApprovalKind.TTS,
                callback_data.token,
            ),
        )
        return

    await _render_tts_outcome(message, outcome)


@router.callback_query(F.data.startswith("pm:t:"))
async def reject_malformed_tts_approval(callback: CallbackQuery) -> None:
    """Reject malformed TTS controls without touching durable state."""
    await callback.answer(
        _("This voice approval is invalid or expired."),
        show_alert=True,
    )


def _funding_text(outcome: PaidMediaAwaitingFunding) -> str:
    if outcome.reason is ReservationRejection.PERSONAL_CONSENT_REQUIRED:
        return _(
            "Shared credits cannot cover this voice. Use your credits once, "
            "or add chat credits. Not charged."
        )
    if outcome.reason is ReservationRejection.WALLET_IN_DEBT:
        return _("Funding is unavailable while this balance is in debt. Not charged.")
    return _("Funding needed: {credits} credits. Not charged.").format(
        credits=outcome.quote.credits
    )


def _not_charged_text(reason: PaidMediaNotChargedReason) -> str:
    if reason is PaidMediaNotChargedReason.INVALID_INPUT:
        return _("The voice request is invalid. Not charged.")
    if reason is PaidMediaNotChargedReason.POLICY_REJECTION:
        return _("The voice request was declined. Not charged.")
    if reason is PaidMediaNotChargedReason.UNUSABLE_OUTPUT:
        return _("The model returned no usable voice. Not charged.")
    if reason is PaidMediaNotChargedReason.QUOTE_EXPIRED:
        return _("The price expired. Send the request again. Not charged.")
    if reason is PaidMediaNotChargedReason.CANCELED:
        return _("Canceled. Not charged.")
    return _("Voice generation did not complete. Not charged.")


async def _render_tts_outcome(
    message: Message,
    outcome: PaidMediaOperationOutcome,
    *,
    retry_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if isinstance(outcome, PaidMediaDelivered):
        if not await _delete_control(message):
            await _edit_control(message, _("Voice delivered."))
        return
    if isinstance(outcome, PaidMediaDeliveryUncertain):
        await _edit_control(
            message,
            _(
                "Delivery is uncertain. The voice may already have arrived. "
                "Check the chat before sending again. No additional charge."
            ),
            reply_markup=paid_media_resend_markup(outcome.resend_token),
        )
        return
    if isinstance(outcome, PaidMediaRefunded):
        await _edit_control(
            message,
            _("Refunded. Delivery failed, so the charged credits were returned."),
        )
        return
    if isinstance(outcome, PaidMediaNotCharged):
        await _edit_control(message, _not_charged_text(outcome.reason))
        return
    if isinstance(outcome, PaidMediaInProgress):
        await _edit_control(
            message,
            {
                "preparing": _("The voice request is still being prepared."),
                "generating": _("The voice is still being generated."),
                "delivering": _("The voice is still being delivered."),
            }[outcome.stage.value],
            reply_markup=retry_markup,
        )
        return
    if isinstance(outcome, PaidMediaAwaitingFunding):
        raise RuntimeError("funding outcome must release its approval lease")
    raise TypeError(f"unsupported TTS outcome: {type(outcome).__name__}")


async def _answer_unavailable(
    callback: CallbackQuery,
    message: Message,
    reason: ResumeUnavailableReason,
) -> None:
    if reason is ResumeUnavailableReason.LEASED:
        await callback.answer(
            _("This voice request is already running."),
            show_alert=True,
        )
        return
    if reason is ResumeUnavailableReason.RESUMED:
        await callback.answer(
            _("This voice request is already complete."),
            show_alert=True,
        )
        return
    await callback.answer(
        _("This voice approval is no longer active."),
        show_alert=True,
    )
    await _edit_control(
        message,
        _("This voice approval is no longer active. Not charged."),
    )


def _purchase_target(chat: ChatModel) -> PurchaseTargetCode:
    if chat.type == "private" or not chat.shared_credit_spending_enabled:
        return PurchaseTargetCode.USER
    return PurchaseTargetCode.CHAT


async def _delete_control(message: Message) -> bool:
    try:
        with suppress_outbound_history():
            return bool(await message.delete())
    except Exception as exc:
        report_exception(
            "tts_approval_control_delete_failed",
            exception=exc,
            level="warning",
        )
        return False


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
            "tts_approval_control_edit_failed",
            exception=exc,
            level="warning",
        )


__all__ = [
    "deny_tts_approval",
    "handle_tts",
    "reject_malformed_tts_approval",
    "router",
    "run_tts_approval",
]
