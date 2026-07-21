"""Telegram TTS commands require durable authenticated approval before work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery

from derp.approvals import (
    ApprovalAuthorizationError,
    ApprovalChoice,
    ApprovalDecision,
    DecisionDisposition,
    DeferredToolSnapshot,
    DeferredToolStatus,
    ResumeLease,
    ResumeUnavailable,
    ResumeUnavailableReason,
)
from derp.approvals.paid_media import (
    PaidMediaApprovalAction,
    PaidMediaApprovalCallback,
    PaidMediaApprovalClaim,
    PaidMediaApprovalCoordinator,
    PaidMediaApprovalKind,
    PaidMediaResumeResult,
)
from derp.billing import CommercePolicy
from derp.billing.telegram import PurchaseCallback, PurchaseTargetCode
from derp.delivery import PaidMediaResendCallback, ProgressStage
from derp.execution import Feature
from derp.features.paid_media_operation import (
    PaidMediaAwaitingFunding,
    PaidMediaDelivered,
    PaidMediaDeliveryUncertain,
)
from derp.features.tts import MAX_TTS_OUTPUT_SECONDS, TtsFeatureService
from derp.features.tts_operation import TTS_COMMAND_TOOL, TTS_PLAN, TtsPaidMediaAdapter
from derp.handlers.tts import (
    deny_tts_approval,
    handle_tts,
    run_tts_approval,
)
from derp.operations import (
    OperationId,
    Quote,
    QuoteEngine,
    QuoteId,
    ReservationRejection,
    TtsQuoteInput,
)

NOW = datetime(2026, 7, 21, 16, tzinfo=UTC)
CALLBACK_TOKEN = "a" * 43


def _callback(message, user) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = user
    callback.answer = AsyncMock()
    callback.bot = message.bot
    return callback


def _quote(*, requester_id, chat_id, telegram_chat_id: int, message_id: int) -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=OperationId.for_command(
            feature=Feature.TTS,
            chat_id=telegram_chat_id,
            message_id=message_id,
        ),
        plan=TTS_PLAN,
        quote_input=TtsQuoteInput(3, MAX_TTS_OUTPUT_SECONDS),
        created_at=NOW,
    )


def _snapshot(
    quote: Quote,
    *,
    requester_id,
    chat_id,
    requester_telegram_id: int,
    chat_telegram_id: int,
    message_id: int,
    status: DeferredToolStatus = DeferredToolStatus.APPROVED,
) -> DeferredToolSnapshot:
    return DeferredToolSnapshot(
        request_id=uuid4(),
        operation_id=quote.operation_id,
        quote_id=quote.id,
        requester_id=requester_id,
        requester_telegram_id=requester_telegram_id,
        chat_id=chat_id,
        chat_telegram_id=chat_telegram_id,
        thread_id=9,
        message_id=message_id,
        tool_name=TTS_COMMAND_TOOL,
        tool_call_id="command-tts",
        status=status,
        expires_at=NOW + timedelta(minutes=10),
        decided_at=NOW if status is not DeferredToolStatus.PENDING else None,
        resumed_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _lease(snapshot: DeferredToolSnapshot) -> ResumeLease:
    return ResumeLease(
        snapshot=snapshot,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
        _validated_arguments={
            "text": "hello world",
            "max_output_seconds": MAX_TTS_OUTPUT_SECONDS,
        },
        _original_history=(MagicMock(),),
    )


def _adapter() -> TtsPaidMediaAdapter:
    service = MagicMock(spec=TtsFeatureService)
    service.synthesize = AsyncMock()
    return TtsPaidMediaAdapter(service)


@pytest.mark.asyncio
async def test_command_only_quotes_and_presents_run_cancel(
    make_message,
    mock_user_model,
    mock_chat_model,
    mock_meta,
) -> None:
    user_id = uuid4()
    chat_id = uuid4()
    message = make_message(
        text="/tts hello world",
        chat_id=-100_123,
        message_id=42,
        message_thread_id=9,
        business_connection_id="business-1",
    )
    user = mock_user_model(user_id=user_id, telegram_id=12345)
    chat = mock_chat_model(chat_id=chat_id, telegram_id=-100_123)
    quote = _quote(
        requester_id=user_id,
        chat_id=chat_id,
        telegram_chat_id=-100_123,
        message_id=42,
    )
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.prepare = AsyncMock(
        return_value=SimpleNamespace(
            quote=quote,
            handle=SimpleNamespace(callback_token=CALLBACK_TOKEN),
        )
    )
    adapter = _adapter()

    await handle_tts(
        message,
        mock_meta(target_text="hello world"),
        workflow,
        adapter,
        user_model=user,
        chat_model=chat,
    )

    prepare = workflow.prepare.await_args.kwargs
    assert prepare["context"].requester_id == user_id
    assert prepare["context"].chat_id == chat_id
    assert prepare["context"].thread_id == 9
    assert prepare["tool_call"].tool_name == TTS_COMMAND_TOOL
    assert prepare["tool_call"].args_as_dict() == {
        "text": "hello world",
        "max_output_seconds": MAX_TTS_OUTPUT_SECONDS,
    }
    assert prepare["adapter"] is adapter
    adapter.service.synthesize.assert_not_awaited()
    reply = message.reply.await_args
    assert reply.args[0] == f"Create this voice message for {quote.credits} credits?"
    buttons = reply.kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["Create voice", "Cancel"]
    callbacks = [
        PaidMediaApprovalCallback.unpack(button.callback_data) for button in buttons
    ]
    assert [item.action for item in callbacks] == [
        PaidMediaApprovalAction.RUN,
        PaidMediaApprovalAction.CANCEL,
    ]
    assert all(item.kind is PaidMediaApprovalKind.TTS for item in callbacks)


@pytest.mark.asyncio
async def test_command_validation_fails_without_quote_or_provider(
    make_message,
    mock_meta,
) -> None:
    message = make_message(text="/tts")
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.prepare = AsyncMock()
    adapter = _adapter()

    await handle_tts(
        message,
        mock_meta(target_text=""),
        workflow,
        adapter,
    )

    assert message.reply.await_args.args[0] == (
        "Send /tts followed by the text to read aloud."
    )
    workflow.prepare.assert_not_awaited()
    adapter.service.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_uses_callback_actor_chat_topic_and_never_runs_provider(
    make_message,
    make_user,
) -> None:
    message = make_message(chat_id=-100_123, message_thread_id=9)
    callback = _callback(message, make_user(id=77))
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.cancel = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.APPLIED)
    )
    workflow.resume = AsyncMock()

    await deny_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=PaidMediaApprovalAction.CANCEL,
            token=CALLBACK_TOKEN,
        ),
        workflow,
    )

    capability = workflow.cancel.await_args.args[0]
    assert capability.requester_telegram_id == 77
    assert capability.chat_telegram_id == -100_123
    assert capability.thread_id == 9
    message.edit_text.assert_awaited_once_with(
        "Canceled. You weren't charged.",
        reply_markup=None,
    )
    workflow.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_callback_scope_does_not_mutate_or_run(
    make_message,
    make_user,
) -> None:
    message = make_message()
    callback = _callback(message, make_user(id=77))
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.approve_and_claim = AsyncMock(
        side_effect=ApprovalAuthorizationError("invalid")
    )
    workflow.resume = AsyncMock()

    await run_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=PaidMediaApprovalAction.RUN,
            token=CALLBACK_TOKEN,
        ),
        workflow,
        _adapter(),
    )

    callback.answer.assert_awaited_once_with(
        "I can't use this request in this chat.",
        show_alert=True,
    )
    message.edit_text.assert_not_awaited()
    workflow.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_approval_renders_durable_delivery_truth(
    make_message,
    make_user,
) -> None:
    message = make_message()
    callback = _callback(message, make_user(id=77))
    quote = _quote(
        requester_id=uuid4(),
        chat_id=uuid4(),
        telegram_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    snapshot = _snapshot(
        quote,
        requester_id=uuid4(),
        chat_id=uuid4(),
        requester_telegram_id=77,
        chat_telegram_id=message.chat.id,
        message_id=message.message_id,
        status=DeferredToolStatus.EXPIRED,
    )
    decision = ApprovalDecision(
        ApprovalChoice.APPROVE,
        DecisionDisposition.EXPIRED,
        snapshot,
    )
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.approve_and_claim = AsyncMock(
        return_value=PaidMediaApprovalClaim(decision, None)
    )
    workflow.reconcile_expired = AsyncMock(
        return_value=PaidMediaDeliveryUncertain(
            quote.operation_id,
            "network_timeout",
            "r" * 43,
        )
    )
    workflow.resume = AsyncMock()

    await run_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=PaidMediaApprovalAction.RUN,
            token=CALLBACK_TOKEN,
        ),
        workflow,
        _adapter(),
    )

    workflow.reconcile_expired.assert_awaited_once_with(snapshot)
    workflow.resume.assert_not_awaited()
    edit = message.edit_text.await_args
    assert "may already be in the chat" in edit.args[0]
    assert "weren't charged" not in edit.args[0]
    button = edit.kwargs["reply_markup"].inline_keyboard[0][0]
    assert PaidMediaResendCallback.unpack(button.callback_data).token == "r" * 43


@pytest.mark.asyncio
async def test_run_uses_lease_and_renders_honest_progress(
    make_message,
    make_user,
    mock_user_model,
    mock_chat_model,
) -> None:
    requester_id = uuid4()
    chat_id = uuid4()
    message = make_message(
        chat_id=-100_123,
        message_id=99,
        message_thread_id=9,
        business_connection_id="business-1",
    )
    callback = _callback(message, make_user(id=77))
    quote = _quote(
        requester_id=requester_id,
        chat_id=chat_id,
        telegram_chat_id=-100_123,
        message_id=99,
    )
    snapshot = _snapshot(
        quote,
        requester_id=requester_id,
        chat_id=chat_id,
        requester_telegram_id=77,
        chat_telegram_id=-100_123,
        message_id=99,
    )
    lease = _lease(snapshot)
    decision = ApprovalDecision(
        ApprovalChoice.APPROVE,
        DecisionDisposition.APPLIED,
        snapshot,
    )
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.approve_and_claim = AsyncMock(
        return_value=PaidMediaApprovalClaim(decision, lease)
    )
    workflow.complete = AsyncMock()
    workflow.release = AsyncMock()

    async def resume(**kwargs) -> PaidMediaResumeResult:
        for stage in ProgressStage:
            await kwargs["progress"](stage)
        return PaidMediaResumeResult(
            PaidMediaDelivered(quote.operation_id, (501,)),
            lease,
        )

    workflow.resume = AsyncMock(side_effect=resume)
    adapter = _adapter()

    await run_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=PaidMediaApprovalAction.RUN,
            token=CALLBACK_TOKEN,
        ),
        workflow,
        adapter,
        user_model=mock_user_model(user_id=requester_id, telegram_id=77),
        chat_model=mock_chat_model(chat_id=chat_id, telegram_id=-100_123),
    )

    callback.answer.assert_awaited_once_with("Started")
    assert [item.args[0] for item in message.edit_text.await_args_list] == [
        "Preparing your voice message...",
        "Creating your voice message...",
        "Sending your voice message...",
    ]
    for edit in message.edit_text.await_args_list:
        button = edit.kwargs["reply_markup"].inline_keyboard[0][0]
        assert PaidMediaApprovalCallback.unpack(button.callback_data).action is (
            PaidMediaApprovalAction.RUN
        )
    workflow.complete.assert_awaited_once_with(lease)
    workflow.release.assert_not_awaited()
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "allow_once", "grant_always"),
    [
        (PaidMediaApprovalAction.USE_PERSONAL_ONCE, True, False),
        (PaidMediaApprovalAction.ALWAYS_HERE, False, True),
    ],
)
async def test_personal_funding_requires_exact_authenticated_action(
    make_message,
    make_user,
    mock_user_model,
    mock_chat_model,
    action: PaidMediaApprovalAction,
    allow_once: bool,
    grant_always: bool,
) -> None:
    requester_id = uuid4()
    chat_id = uuid4()
    message = make_message(
        chat_id=-100_123,
        message_id=100,
        message_thread_id=9,
        business_connection_id=None,
    )
    callback = _callback(message, make_user(id=77))
    quote = _quote(
        requester_id=requester_id,
        chat_id=chat_id,
        telegram_chat_id=-100_123,
        message_id=100,
    )
    snapshot = _snapshot(
        quote,
        requester_id=requester_id,
        chat_id=chat_id,
        requester_telegram_id=77,
        chat_telegram_id=-100_123,
        message_id=100,
    )
    lease = _lease(snapshot)
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.approve_and_claim = AsyncMock(
        return_value=PaidMediaApprovalClaim(
            ApprovalDecision(
                ApprovalChoice.APPROVE,
                DecisionDisposition.IDEMPOTENT,
                snapshot,
            ),
            lease,
        )
    )
    workflow.grant_personal_consent = AsyncMock()
    workflow.resume = AsyncMock(
        return_value=PaidMediaResumeResult(
            PaidMediaDelivered(quote.operation_id, (501,)),
            lease,
        )
    )
    workflow.complete = AsyncMock()
    workflow.release = AsyncMock()
    user = mock_user_model(user_id=requester_id, telegram_id=77)
    chat = mock_chat_model(chat_id=chat_id, telegram_id=-100_123)

    await run_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=action,
            token=CALLBACK_TOKEN,
        ),
        workflow,
        _adapter(),
        user_model=user,
        chat_model=chat,
    )

    assert workflow.resume.await_args.kwargs["allow_personal_once"] is allow_once
    if grant_always:
        workflow.grant_personal_consent.assert_awaited_once_with(
            snapshot,
            requester_id=requester_id,
            chat_id=chat_id,
        )
    else:
        workflow.grant_personal_consent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "shared_spending", "purchase_label", "purchase_target"),
    [
        ("supergroup", True, "Buy chat credits", PurchaseTargetCode.CHAT),
        ("private", False, "Buy credits", PurchaseTargetCode.USER),
        ("supergroup", False, "Buy credits", PurchaseTargetCode.USER),
    ],
)
async def test_funding_panel_offers_policy_aware_consent_and_purchase(
    make_message,
    make_user,
    mock_user_model,
    mock_chat_model,
    chat_type: str,
    shared_spending: bool,
    purchase_label: str,
    purchase_target: PurchaseTargetCode,
) -> None:
    requester_id = uuid4()
    chat_id = uuid4()
    message = make_message(
        chat_id=-100_123,
        message_id=99,
        message_thread_id=9,
        business_connection_id=None,
    )
    callback = _callback(message, make_user(id=77))
    quote = _quote(
        requester_id=requester_id,
        chat_id=chat_id,
        telegram_chat_id=-100_123,
        message_id=99,
    )
    snapshot = _snapshot(
        quote,
        requester_id=requester_id,
        chat_id=chat_id,
        requester_telegram_id=77,
        chat_telegram_id=-100_123,
        message_id=99,
    )
    lease = _lease(snapshot)
    decision = ApprovalDecision(
        ApprovalChoice.APPROVE,
        DecisionDisposition.APPLIED,
        snapshot,
    )
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.approve_and_claim = AsyncMock(
        return_value=PaidMediaApprovalClaim(decision, lease)
    )
    workflow.resume = AsyncMock(
        return_value=PaidMediaResumeResult(
            PaidMediaAwaitingFunding(
                quote.operation_id,
                quote,
                ReservationRejection.PERSONAL_CONSENT_REQUIRED,
            ),
            lease,
        )
    )
    workflow.release = AsyncMock()
    workflow.complete = AsyncMock()

    await run_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=PaidMediaApprovalAction.RUN,
            token=CALLBACK_TOKEN,
        ),
        workflow,
        _adapter(),
        user_model=mock_user_model(user_id=requester_id, telegram_id=77),
        chat_model=mock_chat_model(
            chat_id=chat_id,
            telegram_id=-100_123,
            chat_type=chat_type,
            shared_credit_spending_enabled=shared_spending,
        ),
        commerce_policy=CommercePolicy(public_intake_enabled=True),
    )

    workflow.release.assert_awaited_once_with(lease)
    workflow.complete.assert_not_awaited()
    edit = message.edit_text.await_args
    assert "You weren't charged" in edit.args[0]
    buttons = [
        button for row in edit.kwargs["reply_markup"].inline_keyboard for button in row
    ]
    actions = {
        PaidMediaApprovalCallback.unpack(button.callback_data).action
        for button in buttons
        if button.callback_data.startswith("pm:")
    }
    assert actions == {
        PaidMediaApprovalAction.USE_PERSONAL_ONCE,
        PaidMediaApprovalAction.ALWAYS_HERE,
        PaidMediaApprovalAction.RUN,
        PaidMediaApprovalAction.CANCEL,
    }
    purchase = next(
        PurchaseCallback.unpack(button.callback_data)
        for button in buttons
        if button.text == purchase_label
    )
    assert purchase.target is purchase_target


@pytest.mark.asyncio
async def test_duplicate_completed_callback_never_resumes_provider(
    make_message,
    make_user,
) -> None:
    message = make_message()
    callback = _callback(message, make_user(id=77))
    quote = _quote(
        requester_id=uuid4(),
        chat_id=uuid4(),
        telegram_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    snapshot = _snapshot(
        quote,
        requester_id=uuid4(),
        chat_id=uuid4(),
        requester_telegram_id=77,
        chat_telegram_id=message.chat.id,
        message_id=message.message_id,
        status=DeferredToolStatus.RESUMED,
    )
    decision = ApprovalDecision(
        ApprovalChoice.APPROVE,
        DecisionDisposition.IDEMPOTENT,
        snapshot,
    )
    workflow = MagicMock(spec=PaidMediaApprovalCoordinator)
    workflow.approve_and_claim = AsyncMock(
        return_value=PaidMediaApprovalClaim(
            decision,
            ResumeUnavailable(ResumeUnavailableReason.RESUMED, snapshot),
        )
    )
    workflow.resume = AsyncMock()

    await run_tts_approval(
        callback,
        PaidMediaApprovalCallback(
            kind=PaidMediaApprovalKind.TTS,
            action=PaidMediaApprovalAction.RUN,
            token=CALLBACK_TOKEN,
        ),
        workflow,
        _adapter(),
    )

    workflow.resume.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        "This voice request is already complete.",
        show_alert=True,
    )
    message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_delivery_keeps_no_charge_resend_capability(
    make_message,
) -> None:
    from derp.handlers.tts import _render_tts_outcome

    message = make_message()
    outcome = PaidMediaDeliveryUncertain(
        OperationId(uuid4()),
        "network_timeout",
        "r" * 43,
    )

    await _render_tts_outcome(message, outcome)

    edit = message.edit_text.await_args
    assert "You won't be charged again" in edit.args[0]
    button = edit.kwargs["reply_markup"].inline_keyboard[0][0]
    assert PaidMediaResendCallback.unpack(button.callback_data).token == "r" * 43
