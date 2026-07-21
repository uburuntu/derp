"""Paid-media approvals bind persisted command args to one durable operation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aiogram.types import CallbackQuery
from pydantic_ai import ToolCallPart

from derp.approvals import (
    ApprovalChoice,
    ApprovalDecision,
    ApprovalDecisionConflictError,
    DecisionDisposition,
    DeferredToolApprovalService,
    DeferredToolHandle,
    DeferredToolSnapshot,
    DeferredToolStatus,
    ResumeLease,
)
from derp.approvals.paid_media import (
    PaidMediaApprovalAction,
    PaidMediaApprovalCallback,
    PaidMediaApprovalClaim,
    PaidMediaApprovalCoordinator,
    PaidMediaApprovalError,
    PaidMediaApprovalKind,
    PaidMediaRunContext,
    approval_callback_context,
    pack_paid_media_callback,
)
from derp.delivery import DeliveryMedia, ProgressStage, TelegramMediaKind
from derp.execution import Feature, Succeeded
from derp.features.paid_media_operation import (
    PaidMediaDelivered,
    PaidMediaDeliveryUncertain,
    PaidMediaNotCharged,
    PaidMediaNotChargedReason,
    PaidMediaOperationCoordinator,
    PaidMediaResult,
)
from derp.features.tts import MAX_TTS_OUTPUT_SECONDS, TtsFeatureService, TtsRequest
from derp.features.tts_operation import (
    TTS_COMMAND_TOOL,
    TTS_PLAN,
    DeferredTtsCallError,
    TtsPaidMediaAdapter,
    tts_command_history,
    tts_command_tool_call,
)
from derp.operations import (
    InvalidOperationTransitionError,
    OperationId,
    OperationLedger,
    OperationRequestBinder,
    Quote,
    QuoteEngine,
    QuoteId,
    TtsQuoteInput,
)

NOW = datetime(2026, 7, 21, 14, tzinfo=UTC)
REQUESTER_ID = UUID("1bf3ba69-1032-47e8-b662-29644651389f")
CHAT_ID = UUID("3351bb43-5e03-44cd-85b0-b340af1754a0")
TELEGRAM_CHAT_ID = -100_234
MESSAGE_ID = 42
TOKEN = "a" * 43
BINDING_KEY = b"approval-binding-key".ljust(32, b"!")
VOICE = DeliveryMedia(TelegramMediaKind.VOICE, "audio/ogg", b"voice")


def _context() -> PaidMediaRunContext:
    return PaidMediaRunContext(
        requester_id=REQUESTER_ID,
        requester_telegram_id=77,
        chat_id=CHAT_ID,
        chat_telegram_id=TELEGRAM_CHAT_ID,
        message_id=MESSAGE_ID,
        thread_id=9,
        business_connection_id="business-1",
    )


def _operation_id() -> OperationId:
    return OperationId.for_command(
        feature=Feature.TTS,
        chat_id=TELEGRAM_CHAT_ID,
        message_id=MESSAGE_ID,
    )


def _quote() -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=_operation_id(),
        plan=TTS_PLAN,
        quote_input=TtsQuoteInput(3, MAX_TTS_OUTPUT_SECONDS),
        created_at=NOW,
    )


def _snapshot(
    quote: Quote,
    *,
    status: DeferredToolStatus,
) -> DeferredToolSnapshot:
    return DeferredToolSnapshot(
        request_id=uuid4(),
        operation_id=quote.operation_id,
        quote_id=quote.id,
        requester_id=REQUESTER_ID,
        requester_telegram_id=77,
        chat_id=CHAT_ID,
        chat_telegram_id=TELEGRAM_CHAT_ID,
        thread_id=9,
        message_id=MESSAGE_ID,
        tool_name=TTS_COMMAND_TOOL,
        tool_call_id="command-tts",
        status=status,
        expires_at=NOW + timedelta(minutes=10),
        decided_at=NOW if status is not DeferredToolStatus.PENDING else None,
        resumed_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _lease(quote: Quote, request: TtsRequest) -> ResumeLease:
    snapshot = _snapshot(quote, status=DeferredToolStatus.APPROVED)
    tool_call = tts_command_tool_call(request)
    return ResumeLease(
        snapshot=snapshot,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
        _validated_arguments=tool_call.args_as_dict(),
        _original_history=tts_command_history(request, tool_call),
    )


def _workflow() -> tuple[
    PaidMediaApprovalCoordinator,
    MagicMock,
    MagicMock,
    MagicMock,
]:
    operations = MagicMock(spec=PaidMediaOperationCoordinator)
    approvals = MagicMock(spec=DeferredToolApprovalService)
    ledger = MagicMock(spec=OperationLedger)
    return (
        PaidMediaApprovalCoordinator(
            operations,
            approvals,
            ledger,
            OperationRequestBinder(BINDING_KEY),
        ),
        operations,
        approvals,
        ledger,
    )


def _adapter(*, outcome=None) -> tuple[TtsPaidMediaAdapter, MagicMock]:
    service = MagicMock(spec=TtsFeatureService)
    service.synthesize = AsyncMock(
        return_value=Succeeded(VOICE) if outcome is None else outcome
    )
    return TtsPaidMediaAdapter(service), service


def test_tts_adapter_validates_exact_persisted_schema_and_price() -> None:
    adapter, _ = _adapter()
    request = TtsRequest("hello world", MAX_TTS_OUTPUT_SECONDS)
    call = tts_command_tool_call(request)

    assert adapter.parse_tool_call(call) == request
    quote_input = adapter.quote_input(request)
    assert quote_input.input_tokens == 3
    assert quote_input.output_seconds == MAX_TTS_OUTPUT_SECONDS

    with pytest.raises(DeferredTtsCallError, match="shape"):
        adapter.parse_tool_call(
            ToolCallPart(
                TTS_COMMAND_TOOL,
                {"text": "hello", "max_output_seconds": 30, "voice": "x"},
                "command-tts",
            )
        )
    with pytest.raises(DeferredTtsCallError, match="not a TTS"):
        adapter.parse_tool_call(ToolCallPart("video", {"text": "hello"}, "command-tts"))


@pytest.mark.asyncio
async def test_prepare_quotes_before_persisting_without_provider_execution() -> None:
    workflow, operations, approvals, _ = _workflow()
    adapter, service = _adapter()
    request = TtsRequest("hello world", MAX_TTS_OUTPUT_SECONDS)
    call = tts_command_tool_call(request)
    quote = _quote()
    handle = DeferredToolHandle(
        _snapshot(quote, status=DeferredToolStatus.PENDING),
        TOKEN,
        True,
    )
    events: list[str] = []

    async def ensure_quote(*_: object, **__: object) -> Quote:
        events.append("quote")
        return quote

    async def create_request(**_: object) -> DeferredToolHandle:
        events.append("approval")
        return handle

    operations.ensure_quote = AsyncMock(side_effect=ensure_quote)
    approvals.create_request = AsyncMock(side_effect=create_request)

    prepared = await workflow.prepare(
        context=_context(),
        tool_call=call,
        original_history=tts_command_history(request, call),
        adapter=adapter,
    )

    assert prepared.handle is handle
    assert prepared.quote is quote
    assert events == ["quote", "approval"]
    invocation, plan, quote_input = operations.ensure_quote.await_args.args
    assert invocation.operation_id == _operation_id()
    assert invocation.target.reply_to_message_id == MESSAGE_ID
    assert invocation.target.business_connection_id == "business-1"
    assert invocation.request_binding == OperationRequestBinder(BINDING_KEY).bind(
        Feature.TTS,
        {
            "tool_name": TTS_COMMAND_TOOL,
            "args": {
                "text": "hello world",
                "max_output_seconds": MAX_TTS_OUTPUT_SECONDS,
            },
        },
    )
    assert "hello world" not in repr(invocation)
    assert plan is TTS_PLAN
    assert quote_input == TtsQuoteInput(3, MAX_TTS_OUTPUT_SECONDS)
    create = approvals.create_request.await_args.kwargs
    assert create["operation_id"] == quote.operation_id
    assert create["quote_id"] == quote.id
    assert create["tool_call"] == call
    service.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_uses_only_persisted_request_and_reports_real_stages() -> None:
    workflow, operations, _, _ = _workflow()
    adapter, service = _adapter()
    request = TtsRequest("persisted text", MAX_TTS_OUTPUT_SECONDS)
    quote = _quote()
    lease = _lease(quote, request)
    stages: list[ProgressStage] = []

    async def run(
        invocation,
        plan,
        quote_input,
        provider_request,
        executor,
        **kwargs,
    ):
        assert invocation.operation_id == lease.snapshot.operation_id
        assert plan is TTS_PLAN
        assert quote_input.output_seconds == MAX_TTS_OUTPUT_SECONDS
        assert provider_request == request
        assert kwargs == {"allow_personal_once": True}
        provider_outcome = await executor(plan, provider_request)
        assert provider_outcome == Succeeded(PaidMediaResult((VOICE,)))
        return PaidMediaDelivered(invocation.operation_id, (501,))

    operations.run = AsyncMock(side_effect=run)

    async def report(stage: ProgressStage) -> None:
        stages.append(stage)

    resumed = await workflow.resume(
        lease=lease,
        context=PaidMediaRunContext.from_snapshot(
            lease.snapshot,
            business_connection_id="business-1",
        ),
        adapter=adapter,
        allow_personal_once=True,
        progress=report,
    )

    assert resumed.outcome == PaidMediaDelivered(_operation_id(), (501,))
    assert resumed.lease is lease
    assert stages == [
        ProgressStage.PREPARING,
        ProgressStage.GENERATING,
        ProgressStage.DELIVERING,
    ]
    service.synthesize.assert_awaited_once_with(TTS_PLAN, request)


@pytest.mark.asyncio
async def test_resume_rejects_scope_or_tool_changes_before_operation() -> None:
    workflow, operations, _, _ = _workflow()
    adapter, _ = _adapter()
    request = TtsRequest("persisted text", MAX_TTS_OUTPUT_SECONDS)
    lease = _lease(_quote(), request)

    context = PaidMediaRunContext.from_snapshot(
        lease.snapshot,
        business_connection_id="business-1",
    )
    with pytest.raises(PaidMediaApprovalError, match="runtime context"):
        await workflow.resume(
            lease=lease,
            context=replace(context, requester_telegram_id=999),
            adapter=adapter,
        )

    operations.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_operation_renews_and_returns_latest_lease() -> None:
    workflow, operations, approvals, _ = _workflow()
    adapter, _ = _adapter()
    request = TtsRequest("persisted text", MAX_TTS_OUTPUT_SECONDS)
    quote = _quote()
    lease = _lease(quote, request)
    lease = replace(
        lease,
        lease_expires_at=lease.claimed_at + timedelta(milliseconds=20),
    )
    renewed = replace(
        lease,
        claimed_at=lease.claimed_at + timedelta(milliseconds=10),
        lease_expires_at=lease.claimed_at + timedelta(seconds=1),
    )
    approvals.renew_resume = AsyncMock(return_value=renewed)

    async def run(*_: object, **__: object) -> PaidMediaDelivered:
        await asyncio.sleep(0.03)
        return PaidMediaDelivered(_operation_id(), (501,))

    operations.run = AsyncMock(side_effect=run)
    resumed = await workflow.resume(
        lease=lease,
        context=PaidMediaRunContext.from_snapshot(
            lease.snapshot,
            business_connection_id="business-1",
        ),
        adapter=adapter,
    )

    assert resumed.lease is renewed
    approvals.renew_resume.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_lost_lease_cancels_provider_work_and_releases_claim() -> None:
    workflow, operations, approvals, _ = _workflow()
    adapter, _ = _adapter()
    request = TtsRequest("persisted text", MAX_TTS_OUTPUT_SECONDS)
    lease = _lease(_quote(), request)
    lease = replace(
        lease,
        lease_expires_at=lease.claimed_at + timedelta(milliseconds=10),
    )
    provider_canceled = asyncio.Event()
    approvals.renew_resume = AsyncMock(side_effect=RuntimeError("lease lost"))
    approvals.release_resume = AsyncMock(return_value=True)

    async def run(*_: object, **__: object) -> PaidMediaDelivered:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            provider_canceled.set()
            raise
        raise AssertionError("provider wait should be canceled")

    operations.run = AsyncMock(side_effect=run)
    with pytest.raises(RuntimeError, match="lease lost"):
        await workflow.resume(
            lease=lease,
            context=PaidMediaRunContext.from_snapshot(
                lease.snapshot,
                business_connection_id="business-1",
            ),
            adapter=adapter,
        )

    assert provider_canceled.is_set()
    approvals.release_resume.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_decision_wrappers_cancel_claim_and_grant_exact_scope() -> None:
    workflow, _, approvals, ledger = _workflow()
    quote = _quote()
    pending = _snapshot(quote, status=DeferredToolStatus.PENDING)
    approved = _snapshot(quote, status=DeferredToolStatus.APPROVED)
    denial = ApprovalDecision(
        ApprovalChoice.DENY,
        DecisionDisposition.APPLIED,
        pending,
    )
    approval = ApprovalDecision(
        ApprovalChoice.APPROVE,
        DecisionDisposition.APPLIED,
        approved,
    )
    capability = MagicMock()
    claim = MagicMock()
    approvals.deny = AsyncMock(return_value=denial)
    approvals.approve = AsyncMock(return_value=approval)
    approvals.claim_resume = AsyncMock(return_value=claim)
    approvals.release_resume = AsyncMock(return_value=True)
    approvals.mark_resumed = AsyncMock(return_value=approved)
    ledger.cancel = AsyncMock()
    ledger.grant_personal_consent = AsyncMock()

    assert await workflow.deny(capability) is denial
    ledger.cancel.assert_awaited_once_with(
        quote.operation_id,
        reason="approval_denied",
    )
    assert await workflow.approve_and_claim(capability) == PaidMediaApprovalClaim(
        approval,
        claim,
    )
    await workflow.grant_personal_consent(
        approved,
        requester_id=REQUESTER_ID,
        chat_id=CHAT_ID,
    )
    ledger.grant_personal_consent.assert_awaited_once_with(REQUESTER_ID, CHAT_ID)
    with pytest.raises(PaidMediaApprovalError, match="scope"):
        await workflow.grant_personal_consent(
            approved,
            requester_id=uuid4(),
            chat_id=CHAT_ID,
        )


@pytest.mark.asyncio
async def test_expired_approval_cancels_only_pre_execution_work() -> None:
    workflow, operations, _, ledger = _workflow()
    snapshot = _snapshot(_quote(), status=DeferredToolStatus.EXPIRED)
    ledger.cancel = AsyncMock()
    operations.resume_existing = AsyncMock()

    outcome = await workflow.reconcile_expired(snapshot)

    assert outcome == PaidMediaNotCharged(
        snapshot.operation_id,
        PaidMediaNotChargedReason.QUOTE_EXPIRED,
    )
    ledger.cancel.assert_awaited_once_with(
        snapshot.operation_id,
        reason="approval_expired",
    )
    operations.resume_existing.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_claims_approved_request_before_canceling_operation() -> None:
    workflow, _, approvals, ledger = _workflow()
    quote = _quote()
    lease = _lease(quote, TtsRequest("cancel me", MAX_TTS_OUTPUT_SECONDS))
    denied = replace(
        lease.snapshot,
        status=DeferredToolStatus.DENIED,
        resumed_at=None,
    )
    capability = MagicMock()
    approvals.deny = AsyncMock(
        side_effect=ApprovalDecisionConflictError("already approved")
    )
    approvals.claim_resume = AsyncMock(return_value=lease)
    approvals.cancel_resume = AsyncMock(return_value=denied)
    ledger.cancel = AsyncMock()

    decision = await workflow.cancel(capability)

    assert decision == ApprovalDecision(
        ApprovalChoice.DENY,
        DecisionDisposition.APPLIED,
        denied,
    )
    approvals.claim_resume.assert_awaited_once_with(capability)
    ledger.cancel.assert_awaited_once_with(
        quote.operation_id,
        reason="approval_denied",
    )
    approvals.cancel_resume.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_expired_approval_recovers_post_execution_delivery_truth() -> None:
    workflow, operations, _, ledger = _workflow()
    snapshot = _snapshot(_quote(), status=DeferredToolStatus.EXPIRED)
    expected = PaidMediaDeliveryUncertain(
        snapshot.operation_id,
        "network_timeout",
        "r" * 43,
    )
    ledger.cancel = AsyncMock(
        side_effect=InvalidOperationTransitionError("already captured")
    )
    operations.resume_existing = AsyncMock(return_value=expected)

    assert await workflow.reconcile_expired(snapshot) == expected
    operations.resume_existing.assert_awaited_once_with(snapshot.operation_id)


def test_callback_transport_is_compact_and_scope_comes_from_telegram(
    make_message,
    make_user,
) -> None:
    packed = pack_paid_media_callback(
        PaidMediaApprovalKind.TTS,
        PaidMediaApprovalAction.RUN,
        TOKEN,
    )
    assert len(packed.encode()) <= 64
    assert PaidMediaApprovalCallback.unpack(packed) == PaidMediaApprovalCallback(
        kind=PaidMediaApprovalKind.TTS,
        action=PaidMediaApprovalAction.RUN,
        token=TOKEN,
    )

    message = make_message(chat_id=TELEGRAM_CHAT_ID, message_thread_id=9)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = make_user(id=77)
    _, capability = approval_callback_context(callback, TOKEN)
    assert capability.requester_telegram_id == 77
    assert capability.chat_telegram_id == TELEGRAM_CHAT_ID
    assert capability.thread_id == 9
