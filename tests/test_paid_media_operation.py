"""Paid media coordination is durable across every external-effect boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from derp.artifacts import ArtifactStoreError
from derp.catalog import GoogleModelKey, VideoResolution
from derp.delivery import (
    Delivered,
    DeliveryFailed,
    DeliveryInspection,
    DeliveryMedia,
    DeliveryService,
    DeliveryState,
    DeliveryTarget,
    DeliveryUncertain,
    PreparedDelivery,
    ProgressStage,
    TelegramMediaKind,
)
from derp.execution import (
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.paid_media_operation import (
    PaidMediaAwaitingFunding,
    PaidMediaDelivered,
    PaidMediaDeliveryUncertain,
    PaidMediaInProgress,
    PaidMediaInvocation,
    PaidMediaNotCharged,
    PaidMediaNotChargedReason,
    PaidMediaOperationCoordinator,
    PaidMediaRefunded,
    PaidMediaResult,
)
from derp.operations import (
    FundingAuthorization,
    InvalidOperationTransitionError,
    InventoryAllocation,
    OperationId,
    OperationLedger,
    OperationSnapshot,
    OperationState,
    Quote,
    QuoteEngine,
    QuoteId,
    ReservationRejected,
    ReservationRejection,
    ReservedOperation,
    SettlementResult,
    TtsQuoteInput,
    VideoGenerateQuoteInput,
    WalletOwner,
    WalletOwnerKind,
)

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
OPERATION_ID = OperationId(UUID("a19be8fc-1009-43cf-8229-66ae89a35115"))
REQUESTER_ID = UUID("af2caa28-ac45-4cc0-8b8d-f0d6ffdfa876")
CHAT_ID = UUID("7843f0ca-9f25-45eb-b3f2-c3953024d655")
TTS_PLAN = plan_execution(Feature.TTS, GoogleModelKey.TTS)
VIDEO_PLAN = plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST)
TTS_QUOTE_INPUT = TtsQuoteInput(input_tokens=120, output_seconds=30)
VIDEO_QUOTE_INPUT = VideoGenerateQuoteInput(
    input_tokens=120,
    duration_seconds=6,
    resolution=VideoResolution.HD_720P,
)
AUDIO_RESULT = PaidMediaResult(
    (
        DeliveryMedia(
            TelegramMediaKind.AUDIO,
            "audio/mpeg",
            b"generated-audio",
        ),
    ),
    "Generated speech",
)
VIDEO_RESULT = PaidMediaResult(
    (
        DeliveryMedia(
            TelegramMediaKind.VIDEO,
            "video/mp4",
            b"generated-video",
        ),
    ),
)


def _inspection(
    state: DeliveryState,
    *,
    message_ids: tuple[int, ...] = (),
    last_error_code: str | None = None,
) -> DeliveryInspection:
    return DeliveryInspection(
        operation_id=OPERATION_ID,
        state=state,
        target=DeliveryTarget(-100123, 7, 42),
        attempt_count=1,
        artifact_count=1,
        expires_at=NOW + timedelta(hours=1),
        updated_at=NOW,
        message_ids=message_ids,
        last_error_code=last_error_code,
    )


@dataclass(frozen=True, slots=True)
class Environment:
    coordinator: PaidMediaOperationCoordinator
    ledger: MagicMock
    delivery: MagicMock
    executor: AsyncMock
    invocation: PaidMediaInvocation


@pytest.fixture
def env() -> Environment:
    ledger = MagicMock(spec=OperationLedger)
    delivery = MagicMock(spec=DeliveryService)
    executor = AsyncMock(return_value=Succeeded(AUDIO_RESULT))
    invocation = PaidMediaInvocation(
        operation_id=OPERATION_ID,
        request_key="command:tts:42",
        requester_id=REQUESTER_ID,
        chat_id=CHAT_ID,
        thread_id=7,
        target=DeliveryTarget(-100123, 7, 42),
        estimated_input_tokens=120,
        request_binding="a" * 64,
    )

    async def ensure_quote(quote: Quote, **_: object) -> Quote:
        return quote

    ledger.ensure_quote = AsyncMock(side_effect=ensure_quote)
    ledger.reserve = AsyncMock(return_value=_reservation(idempotent=False))
    ledger.mark_executing = AsyncMock(
        return_value=SettlementResult(
            OPERATION_ID,
            OperationState.EXECUTING,
            changed=True,
        )
    )
    ledger.capture_persisted_result = AsyncMock(
        return_value=SettlementResult(
            OPERATION_ID,
            OperationState.CAPTURED,
            changed=True,
        )
    )
    ledger.release = AsyncMock(
        return_value=SettlementResult(
            OPERATION_ID,
            OperationState.RELEASED,
            changed=True,
        )
    )
    delivery.persist_result = AsyncMock(
        return_value=PreparedDelivery(
            OPERATION_ID,
            uuid4(),
            "opaque-resend-token",
            artifact_count=1,
        )
    )
    delivery.mark_ready = AsyncMock()
    delivery.deliver = AsyncMock(return_value=Delivered((501,)))
    delivery.inspect = AsyncMock(return_value=_inspection(DeliveryState.PENDING))
    delivery.issue_resend_token = AsyncMock(return_value="reissued-resend-token")

    return Environment(
        PaidMediaOperationCoordinator(
            ledger,
            QuoteEngine(),
            delivery,
            clock=lambda: NOW,
            quote_id_factory=lambda: QuoteId(
                UUID("ce5e0673-b476-4aa5-96df-997d956b5bc5")
            ),
        ),
        ledger,
        delivery,
        executor,
        invocation,
    )


def _reservation(*, idempotent: bool) -> ReservedOperation:
    return ReservedOperation(
        OPERATION_ID,
        InventoryAllocation(
            WalletOwner(WalletOwnerKind.USER, REQUESTER_ID),
            allowance_credits=0,
            purchased_credits=10,
        ),
        FundingAuthorization.PRIVATE,
        idempotent=idempotent,
    )


def _quote() -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=OPERATION_ID,
        plan=TTS_PLAN,
        quote_input=TTS_QUOTE_INPUT,
        created_at=NOW,
    )


def _snapshot(
    *,
    state: OperationState,
    delivery_state: DeliveryState = DeliveryState.NOT_READY,
    artifact_count: int | None = None,
    terminal_reason: str | None = None,
) -> OperationSnapshot:
    result_metadata = (
        {"artifact_count": artifact_count} if artifact_count is not None else {}
    )
    provider_claimed = state not in {OperationState.QUOTED, OperationState.RESERVED}
    was_captured = state in {OperationState.CAPTURED, OperationState.REVERSED}
    return OperationSnapshot(
        operation_id=OPERATION_ID,
        quote=_quote(),
        provider_model_id=TTS_PLAN.model.provider_model_id,
        request_key="command:tts:42",
        requester_id=REQUESTER_ID,
        chat_id=CHAT_ID,
        thread_id=7,
        pricing_input={},
        state=state,
        delivery_state=delivery_state,
        wallet_owner=WalletOwner(WalletOwnerKind.USER, REQUESTER_ID),
        funding_authorization=FundingAuthorization.PRIVATE,
        result_metadata=result_metadata,
        terminal_reason=terminal_reason,
        reserved_at=NOW,
        execution_started_at=NOW if provider_claimed else None,
        captured_at=NOW if was_captured else None,
        released_at=NOW if state is OperationState.RELEASED else None,
        reversed_at=NOW if state is OperationState.REVERSED else None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_sensitive_provider_result_is_excluded_from_representations(
    env: Environment,
) -> None:
    uncertain = PaidMediaDeliveryUncertain(
        OPERATION_ID,
        "network_timeout",
        "private-resend-capability",
    )

    assert "generated-audio" not in repr(AUDIO_RESULT)
    assert "Generated speech" not in repr(AUDIO_RESULT)
    assert "a" * 64 not in repr(env.invocation)
    assert "private-resend-capability" not in repr(uncertain)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"request_key": " "}, "request_key must not be blank"),
        ({"estimated_input_tokens": -1}, "must not be negative"),
        ({"request_binding": "not-a-binding"}, "lowercase hexadecimal"),
        (
            {"target": DeliveryTarget(-100123, 8, 42)},
            "thread scopes must match",
        ),
    ],
)
def test_invocation_rejects_unstable_or_ambiguous_inputs(
    env: Environment,
    changes: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        replace(env.invocation, **changes)


@pytest.mark.asyncio
async def test_new_operation_orders_every_external_effect(env: Environment) -> None:
    events: list[str] = []

    async def ensure_quote(quote: Quote, **_: object) -> Quote:
        events.append("quote")
        return quote

    async def reserve(*_: object, **__: object) -> ReservedOperation:
        events.append("reserve")
        return _reservation(idempotent=False)

    async def claim(*_: object) -> SettlementResult:
        events.append("claim")
        return SettlementResult(OPERATION_ID, OperationState.EXECUTING, True)

    async def execute(*_: object) -> Succeeded[PaidMediaResult]:
        events.append("provider")
        return Succeeded(AUDIO_RESULT)

    async def persist(*_: object, **__: object) -> PreparedDelivery:
        events.append("persist")
        return PreparedDelivery(OPERATION_ID, uuid4(), "resend-token", 1)

    async def capture(*_: object) -> SettlementResult:
        events.append("capture")
        return SettlementResult(OPERATION_ID, OperationState.CAPTURED, True)

    async def ready(*_: object) -> None:
        events.append("ready")

    async def deliver(*_: object) -> Delivered:
        events.append("deliver")
        return Delivered((501,))

    env.ledger.ensure_quote.side_effect = ensure_quote
    env.ledger.reserve.side_effect = reserve
    env.ledger.mark_executing.side_effect = claim
    env.executor.side_effect = execute
    env.delivery.persist_result.side_effect = persist
    env.ledger.capture_persisted_result.side_effect = capture
    env.delivery.mark_ready.side_effect = ready
    env.delivery.deliver.side_effect = deliver

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        "private provider request",
        env.executor,
    )

    assert outcome == PaidMediaDelivered(OPERATION_ID, (501,))
    assert events == [
        "quote",
        "reserve",
        "claim",
        "provider",
        "persist",
        "capture",
        "ready",
        "deliver",
    ]
    env.ledger.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_only_persists_content_free_commercial_identity(
    env: Environment,
) -> None:
    quote = await env.coordinator.ensure_quote(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
    )

    assert quote.operation_id == OPERATION_ID
    call = env.ledger.ensure_quote.await_args
    assert call.kwargs["request_key"] == "command:tts:42"
    assert call.kwargs["requester_id"] == REQUESTER_ID
    assert call.kwargs["chat_id"] == CHAT_ID
    assert call.kwargs["thread_id"] == 7
    assert call.kwargs["pricing_input"] == {
        "input_tokens": 120,
        "output_seconds": 30,
        "request_binding": "a" * 64,
        "delivery_fingerprint": call.kwargs["pricing_input"]["delivery_fingerprint"],
    }
    assert len(call.kwargs["pricing_input"]["delivery_fingerprint"]) == 64
    env.ledger.reserve.assert_not_awaited()
    env.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_uses_same_core_with_exact_plan_and_quote(env: Environment) -> None:
    invocation = replace(
        env.invocation,
        request_key="command:video:42",
    )
    env.executor.return_value = Succeeded(VIDEO_RESULT)

    outcome = await env.coordinator.run(
        invocation,
        VIDEO_PLAN,
        VIDEO_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaDelivered(OPERATION_ID, (501,))
    proposed = env.ledger.ensure_quote.await_args.args[0]
    assert proposed.key.feature is Feature.VIDEO_GENERATE
    assert proposed.key.model_key is GoogleModelKey.VIDEO_FAST
    assert env.ledger.ensure_quote.await_args.kwargs["pricing_input"] == {
        "input_tokens": 120,
        "duration_seconds": 6,
        "resolution": "720p",
        "request_binding": "a" * 64,
        "delivery_fingerprint": env.ledger.ensure_quote.await_args.kwargs[
            "pricing_input"
        ]["delivery_fingerprint"],
    }
    env.delivery.persist_result.assert_awaited_once_with(
        OPERATION_ID,
        media=VIDEO_RESULT.media,
        target=invocation.target,
        caption=None,
    )


@pytest.mark.asyncio
async def test_mismatched_plan_or_token_estimate_fails_before_effects(
    env: Environment,
) -> None:
    with pytest.raises(ValueError, match="cannot price"):
        await env.coordinator.run(
            env.invocation,
            VIDEO_PLAN,
            TTS_QUOTE_INPUT,
            object(),
            env.executor,
        )
    with pytest.raises(ValueError, match="token estimates must match"):
        await env.coordinator.run(
            env.invocation,
            TTS_PLAN,
            TtsQuoteInput(121, 30),
            object(),
            env.executor,
        )

    env.ledger.ensure_quote.assert_not_awaited()
    env.ledger.reserve.assert_not_awaited()
    env.executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
        ReservationRejection.INSUFFICIENT_FUNDS,
        ReservationRejection.WALLET_IN_DEBT,
    ],
)
async def test_actionable_reservation_rejection_awaits_funding(
    env: Environment,
    reason: ReservationRejection,
) -> None:
    env.ledger.reserve.return_value = ReservationRejected(OPERATION_ID, reason)

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert isinstance(outcome, PaidMediaAwaitingFunding)
    assert outcome.operation_id == OPERATION_ID
    assert outcome.reason is reason
    env.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_fallback_is_enabled_only_by_explicit_call_flag(
    env: Environment,
) -> None:
    await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
        allow_personal_once=True,
    )

    with pytest.raises(TypeError, match="must be a boolean"):
        await env.coordinator.run(
            env.invocation,
            TTS_PLAN,
            TTS_QUOTE_INPUT,
            object(),
            env.executor,
            allow_personal_once="yes",  # type: ignore[arg-type]
        )

    env.ledger.reserve.assert_awaited_once_with(
        OPERATION_ID,
        allow_personal_once=True,
    )


@pytest.mark.asyncio
async def test_expired_quote_is_not_charged(env: Environment) -> None:
    env.ledger.reserve.return_value = ReservationRejected(
        OPERATION_ID,
        ReservationRejection.QUOTE_EXPIRED,
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaNotCharged(
        OPERATION_ID,
        PaidMediaNotChargedReason.QUOTE_EXPIRED,
    )
    env.executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "reason", "release_suffix"),
    [
        (
            Rejected(RejectionReason.INVALID_INPUT),
            PaidMediaNotChargedReason.INVALID_INPUT,
            "invalid_input",
        ),
        (
            Rejected(RejectionReason.POLICY),
            PaidMediaNotChargedReason.POLICY_REJECTION,
            "policy",
        ),
        (
            Rejected(RejectionReason.UNUSABLE_OUTPUT),
            PaidMediaNotChargedReason.UNUSABLE_OUTPUT,
            "unusable_output",
        ),
        (
            Failed(FailureReason.PROVIDER_ERROR),
            PaidMediaNotChargedReason.PROVIDER_FAILURE,
            "provider_error",
        ),
    ],
)
async def test_provider_outcome_releases_without_capture(
    env: Environment,
    provider_outcome: object,
    reason: PaidMediaNotChargedReason,
    release_suffix: str,
) -> None:
    env.executor.return_value = provider_outcome

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaNotCharged(OPERATION_ID, reason)
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason=f"tts_{release_suffix}",
    )
    env.delivery.persist_result.assert_not_awaited()
    env.ledger.capture_persisted_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_exception_is_reported_privately_and_released(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = MagicMock()
    monkeypatch.setattr(
        "derp.features.paid_media_operation.report_exception",
        report,
    )
    private_error = RuntimeError("private provider response")
    env.executor.side_effect = private_error

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaNotCharged(
        OPERATION_ID,
        PaidMediaNotChargedReason.PROVIDER_FAILURE,
    )
    report.assert_called_once_with(
        "paid_media_provider_failed",
        exception=private_error,
        level="warning",
        operation_id=str(OPERATION_ID),
        feature="tts",
    )
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="tts_provider_failure",
    )


@pytest.mark.asyncio
async def test_provider_timeout_is_enforced_and_released(env: Environment) -> None:
    coordinator = PaidMediaOperationCoordinator(
        env.ledger,
        QuoteEngine(),
        env.delivery,
        clock=lambda: NOW,
        provider_timeout=timedelta(microseconds=1),
    )

    async def blocked(*_: object) -> Succeeded[PaidMediaResult]:
        await asyncio.sleep(60)
        return Succeeded(AUDIO_RESULT)

    outcome = await coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        blocked,
    )

    assert outcome == PaidMediaNotCharged(
        OPERATION_ID,
        PaidMediaNotChargedReason.PROVIDER_TIMEOUT,
    )
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="tts_provider_timeout",
    )


@pytest.mark.asyncio
async def test_task_cancellation_never_claims_provider_failure(
    env: Environment,
) -> None:
    env.executor.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await env.coordinator.run(
            env.invocation,
            TTS_PLAN,
            TTS_QUOTE_INPUT,
            object(),
            env.executor,
        )

    env.ledger.release.assert_not_awaited()
    env.ledger.capture_persisted_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        VIDEO_RESULT,
        PaidMediaResult(
            (
                DeliveryMedia(TelegramMediaKind.VOICE, "audio/ogg", b"one"),
                DeliveryMedia(TelegramMediaKind.VOICE, "audio/ogg", b"two"),
            )
        ),
    ],
)
async def test_unusable_provider_media_is_released_before_persistence(
    env: Environment,
    result: PaidMediaResult,
) -> None:
    env.executor.return_value = Succeeded(result)

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaNotCharged(
        OPERATION_ID,
        PaidMediaNotChargedReason.UNUSABLE_OUTPUT,
    )
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="tts_unusable_output",
    )
    env.delivery.persist_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_output_quote_rejects_a_multi_video_result(
    env: Environment,
) -> None:
    env.executor.return_value = Succeeded(
        PaidMediaResult(
            (
                DeliveryMedia(TelegramMediaKind.VIDEO, "video/mp4", b"one"),
                DeliveryMedia(TelegramMediaKind.VIDEO, "video/mp4", b"two"),
            )
        )
    )

    outcome = await env.coordinator.run(
        env.invocation,
        VIDEO_PLAN,
        VIDEO_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaNotCharged(
        OPERATION_ID,
        PaidMediaNotChargedReason.UNUSABLE_OUTPUT,
    )
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="video_generate_unusable_output",
    )
    env.delivery.persist_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_storage_failure_releases_without_capture(
    env: Environment,
) -> None:
    env.delivery.persist_result.side_effect = ArtifactStoreError(
        "private storage failure"
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaNotCharged(
        OPERATION_ID,
        PaidMediaNotChargedReason.RESULT_STORAGE_FAILURE,
    )
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="tts_result_storage_failed",
    )
    env.ledger.capture_persisted_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_result_identity_mismatch_never_captures(
    env: Environment,
) -> None:
    env.delivery.persist_result.return_value = PreparedDelivery(
        OperationId(uuid4()),
        uuid4(),
        "resend-token",
        1,
    )

    with pytest.raises(RuntimeError, match="belongs to another operation"):
        await env.coordinator.run(
            env.invocation,
            TTS_PLAN,
            TTS_QUOTE_INPUT,
            object(),
            env.executor,
        )

    env.ledger.capture_persisted_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_provider_claim_never_replays_provider(env: Environment) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(state=OperationState.EXECUTING)
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(OPERATION_ID, ProgressStage.GENERATING)
    env.ledger.mark_executing.assert_not_awaited()
    env.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_result_resumes_capture_without_provider_replay(
    env: Environment,
) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.EXECUTING,
            artifact_count=1,
        )
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaDelivered(OPERATION_ID, (501,))
    env.executor.assert_not_awaited()
    env.ledger.capture_persisted_result.assert_awaited_once_with(OPERATION_ID)
    env.delivery.mark_ready.assert_awaited_once_with(OPERATION_ID)


@pytest.mark.asyncio
async def test_incomplete_persisted_result_waits_for_reconciliation(
    env: Environment,
) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.EXECUTING,
            artifact_count=1,
        )
    )
    env.ledger.capture_persisted_result.return_value = None

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(
        OPERATION_ID,
        ProgressStage.PREPARING,
        "result_reconciliation_pending",
    )
    env.executor.assert_not_awaited()
    env.delivery.mark_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_identity_mismatch_fails_before_delivery(
    env: Environment,
) -> None:
    env.ledger.capture_persisted_result.return_value = SettlementResult(
        OperationId(uuid4()),
        OperationState.CAPTURED,
        True,
    )

    with pytest.raises(RuntimeError, match="settled a different operation"):
        await env.coordinator.run(
            env.invocation,
            TTS_PLAN,
            TTS_QUOTE_INPUT,
            object(),
            env.executor,
        )

    env.delivery.mark_ready.assert_not_awaited()
    env.delivery.deliver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_state", "ready_calls"),
    [
        (DeliveryState.NOT_READY, 1),
        (DeliveryState.PENDING, 0),
    ],
)
async def test_captured_result_resumes_delivery_only(
    env: Environment,
    delivery_state: DeliveryState,
    ready_calls: int,
) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.CAPTURED,
            delivery_state=delivery_state,
            artifact_count=1,
        )
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaDelivered(OPERATION_ID, (501,))
    assert env.delivery.mark_ready.await_count == ready_calls
    env.executor.assert_not_awaited()
    env.ledger.capture_persisted_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_delivery_uncertainty_keeps_prepared_resend_token(
    env: Environment,
) -> None:
    env.delivery.deliver.return_value = DeliveryUncertain("network_timeout")

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaDeliveryUncertain(
        OPERATION_ID,
        "network_timeout",
        "opaque-resend-token",
    )
    env.delivery.issue_resend_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_capture_readiness_failure_returns_reconciling_state(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("private database detail")
    report = MagicMock()
    monkeypatch.setattr(
        "derp.features.paid_media_operation.report_exception",
        report,
    )
    env.delivery.mark_ready.side_effect = failure
    env.ledger.get_snapshot.return_value = _snapshot(
        state=OperationState.CAPTURED,
        delivery_state=DeliveryState.NOT_READY,
        artifact_count=1,
    )
    env.delivery.inspect.return_value = _inspection(DeliveryState.NOT_READY)

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(
        OPERATION_ID,
        ProgressStage.DELIVERING,
        "delivery_reconciliation_pending",
    )
    report.assert_called_once_with(
        "paid_media_delivery_recovery_needed",
        exception=failure,
        level="warning",
        operation_id=str(OPERATION_ID),
        feature="tts",
    )


@pytest.mark.asyncio
async def test_post_acknowledgement_exception_recovers_delivered_truth(
    env: Environment,
) -> None:
    env.delivery.deliver.side_effect = RuntimeError("database unavailable")
    env.ledger.get_snapshot.return_value = _snapshot(
        state=OperationState.CAPTURED,
        delivery_state=DeliveryState.DELIVERED,
        artifact_count=1,
    )
    env.delivery.inspect.return_value = _inspection(
        DeliveryState.DELIVERED,
        message_ids=(601,),
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaDelivered(OPERATION_ID, (601,))


@pytest.mark.asyncio
async def test_restart_uncertainty_reissues_token_without_provider_replay(
    env: Environment,
) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.CAPTURED,
            delivery_state=DeliveryState.PENDING,
            artifact_count=1,
        )
    )
    env.delivery.deliver.return_value = DeliveryUncertain("network_timeout")

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaDeliveryUncertain(
        OPERATION_ID,
        "network_timeout",
        "reissued-resend-token",
    )
    env.executor.assert_not_awaited()
    env.delivery.issue_resend_token.assert_awaited_once_with(OPERATION_ID)


@pytest.mark.asyncio
async def test_interrupted_delivery_remains_in_progress_without_resend(
    env: Environment,
) -> None:
    env.delivery.deliver.return_value = DeliveryUncertain(
        "attempt_in_progress_or_interrupted"
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(
        OPERATION_ID,
        ProgressStage.DELIVERING,
        "attempt_in_progress_or_interrupted",
    )
    env.delivery.issue_resend_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_retryable_delivery_failure_remains_in_progress(
    env: Environment,
) -> None:
    env.delivery.deliver.return_value = DeliveryFailed("TelegramRetryAfter", True)

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(
        OPERATION_ID,
        ProgressStage.DELIVERING,
        "TelegramRetryAfter",
    )


@pytest.mark.asyncio
async def test_terminal_delivery_failure_is_refunded_only_after_reversal(
    env: Environment,
) -> None:
    env.delivery.deliver.return_value = DeliveryFailed("TelegramBadRequest", False)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.REVERSED,
            delivery_state=DeliveryState.FAILED,
            artifact_count=1,
            terminal_reason="delivery_TelegramBadRequest",
        )
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaRefunded(OPERATION_ID, "TelegramBadRequest")


@pytest.mark.asyncio
async def test_terminal_delivery_without_reversal_stays_in_reconciliation(
    env: Environment,
) -> None:
    env.delivery.deliver.return_value = DeliveryFailed("TelegramBadRequest", False)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.CAPTURED,
            delivery_state=DeliveryState.FAILED,
            artifact_count=1,
        )
    )
    env.delivery.inspect.return_value = _inspection(
        DeliveryState.FAILED,
        last_error_code="TelegramBadRequest",
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(
        OPERATION_ID,
        ProgressStage.DELIVERING,
        "TelegramBadRequest",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            OperationState.REVERSED,
            PaidMediaRefunded(OPERATION_ID, "terminal_reason"),
        ),
        (
            OperationState.RELEASED,
            PaidMediaNotCharged(
                OPERATION_ID,
                PaidMediaNotChargedReason.RELEASED,
            ),
        ),
        (
            OperationState.CANCELED,
            PaidMediaNotCharged(
                OPERATION_ID,
                PaidMediaNotChargedReason.CANCELED,
            ),
        ),
        (
            OperationState.FAILED,
            PaidMediaNotCharged(OPERATION_ID, PaidMediaNotChargedReason.FAILED),
        ),
    ],
)
async def test_terminal_duplicate_maps_existing_state_without_provider(
    env: Environment,
    state: OperationState,
    expected: object,
) -> None:
    env.ledger.reserve.return_value = ReservationRejected(
        OPERATION_ID,
        ReservationRejection.OPERATION_TERMINAL,
    )
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=state,
            terminal_reason="terminal_reason",
        )
    )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == expected
    env.executor.assert_not_awaited()
    env.ledger.mark_executing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition_conflict", [False, True])
async def test_lost_execution_claim_resumes_without_provider_replay(
    env: Environment,
    transition_conflict: bool,
) -> None:
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(state=OperationState.EXECUTING)
    )
    if transition_conflict:
        env.ledger.mark_executing.side_effect = InvalidOperationTransitionError(
            "lost claim"
        )
    else:
        env.ledger.mark_executing.return_value = SettlementResult(
            OPERATION_ID,
            OperationState.EXECUTING,
            changed=False,
        )

    outcome = await env.coordinator.run(
        env.invocation,
        TTS_PLAN,
        TTS_QUOTE_INPUT,
        object(),
        env.executor,
    )

    assert outcome == PaidMediaInProgress(OPERATION_ID, ProgressStage.GENERATING)
    env.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_violation_releases_then_fails_fast(env: Environment) -> None:
    env.executor.return_value = object()

    with pytest.raises(TypeError, match="supported Outcome"):
        await env.coordinator.run(
            env.invocation,
            TTS_PLAN,
            TTS_QUOTE_INPUT,
            object(),
            env.executor,
        )

    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="tts_provider_contract_error",
    )
    env.ledger.capture_persisted_result.assert_not_awaited()


def test_operation_telemetry_has_economics_and_controlled_outcome_only() -> None:
    span = MagicMock()
    quote = _quote()

    from derp.operations import record_operation_quote

    record_operation_quote(
        span,
        quote,
        provider_model_id=TTS_PLAN.model.provider_model_id,
    )
    PaidMediaOperationCoordinator._record_outcome(
        span,
        PaidMediaDelivered(OPERATION_ID, (501,)),
        authorization=FundingAuthorization.PRIVATE,
    )

    economics = span.set_attributes.call_args_list[0].args[0]
    assert economics["derp.operation.capability"] == "tts"
    assert economics["gen_ai.request.model"] == TTS_PLAN.model.provider_model_id
    assert isinstance(economics["derp.operation.estimated_provider_cost_usd"], float)
    assert not {"prompt", "caption", "content", "request"} & economics.keys()
    assert span.set_attributes.call_args_list[1].args[0] == {
        "derp.operation.authorization": "private",
        "derp.operation.outcome": "delivered",
        "derp.operation.terminal_outcome": "none",
    }
