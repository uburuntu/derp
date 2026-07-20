"""Paid image coordination is idempotent across every external-effect boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from derp.artifacts import ArtifactStoreError
from derp.catalog import GoogleModelKey
from derp.delivery import (
    Delivered,
    DeliveryFailed,
    DeliveryService,
    DeliveryState,
    DeliveryTarget,
    DeliveryUncertain,
    PreparedDelivery,
    ProgressStage,
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
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageFeatureService,
    ImageGenerateRequest,
    ImageInProgress,
    ImageInvocation,
    ImageNotCharged,
    ImageNotChargedReason,
    ImageOperationCoordinator,
    ImageOutput,
    ImageRefunded,
    MediaContent,
)
from derp.media import MediaFamily
from derp.operations import (
    FundingAuthorization,
    ImageGenerateQuoteInput,
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
    WalletOwner,
    WalletOwnerKind,
)

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)
OPERATION_ID = OperationId(UUID("cfd7bfa1-d45d-4e13-93f7-e11dccbf149c"))
REQUESTER_ID = UUID("6a7555e7-ebc1-42eb-9900-24825f3a8a95")
CHAT_ID = UUID("2c10cb89-bbbb-4a6c-a814-1be35a3973b4")
PLAN = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
REQUEST = ImageGenerateRequest("Draw a quiet observatory", style="linocut")
OUTPUT = ImageOutput(
    (MediaContent(MediaFamily.IMAGE, "image/png", b"generated-image"),)
)


@dataclass(frozen=True, slots=True)
class Environment:
    coordinator: ImageOperationCoordinator
    ledger: MagicMock
    image_service: MagicMock
    delivery_service: MagicMock
    invocation: ImageInvocation


@pytest.fixture
def env() -> Environment:
    ledger = MagicMock(spec=OperationLedger)
    image_service = MagicMock(spec=ImageFeatureService)
    delivery_service = MagicMock(spec=DeliveryService)
    invocation = ImageInvocation(
        operation_id=OPERATION_ID,
        request_key="command:image:42",
        requester_id=REQUESTER_ID,
        chat_id=CHAT_ID,
        thread_id=7,
        target=DeliveryTarget(-100123, 7, 42),
        input_tokens=120,
        caption="Created image",
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
    ledger.capture = AsyncMock(
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
    image_service.generate = AsyncMock(return_value=Succeeded(OUTPUT))
    image_service.edit = AsyncMock()
    delivery_service.persist_result = AsyncMock(
        return_value=PreparedDelivery(
            OPERATION_ID,
            uuid4(),
            "opaque-resend-token",
            artifact_count=1,
        )
    )
    delivery_service.mark_ready = AsyncMock()
    delivery_service.deliver = AsyncMock(return_value=Delivered((501,)))
    delivery_service.issue_resend_token = AsyncMock(
        return_value="reissued-resend-token"
    )

    return Environment(
        coordinator=ImageOperationCoordinator(
            ledger,
            QuoteEngine(),
            image_service,
            delivery_service,
            clock=lambda: NOW,
            quote_id_factory=lambda: QuoteId(
                UUID("ee238d9a-b370-4c3e-80b8-5b8dcf8b2943")
            ),
        ),
        ledger=ledger,
        image_service=image_service,
        delivery_service=delivery_service,
        invocation=invocation,
    )


def _quote() -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=OPERATION_ID,
        plan=PLAN,
        quote_input=ImageGenerateQuoteInput(120, REQUEST.resolution),
        created_at=NOW,
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
    return OperationSnapshot(
        operation_id=OPERATION_ID,
        quote=_quote(),
        provider_model_id=PLAN.model.provider_model_id,
        request_key="command:image:42",
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
        execution_started_at=(
            NOW
            if state not in {OperationState.QUOTED, OperationState.RESERVED}
            else None
        ),
        captured_at=(NOW if state is OperationState.CAPTURED else None),
        released_at=(NOW if state is OperationState.RELEASED else None),
        reversed_at=(NOW if state is OperationState.REVERSED else None),
        created_at=NOW,
        updated_at=NOW,
    )


def test_sensitive_delivery_values_are_excluded_from_representations(
    env: Environment,
) -> None:
    outcome = ImageDeliveryUncertain(
        OPERATION_ID,
        "network_timeout",
        "private-resend-capability",
    )

    assert "Created image" not in repr(env.invocation)
    assert "private-resend-capability" not in repr(outcome)


@pytest.mark.asyncio
async def test_new_operation_persists_output_before_capture_and_delivery(
    env: Environment,
) -> None:
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

    async def execute(*_: object) -> Succeeded[ImageOutput]:
        events.append("provider")
        return Succeeded(OUTPUT)

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
    env.image_service.generate.side_effect = execute
    env.delivery_service.persist_result.side_effect = persist
    env.ledger.capture.side_effect = capture
    env.delivery_service.mark_ready.side_effect = ready
    env.delivery_service.deliver.side_effect = deliver

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageDelivered(OPERATION_ID, (501,))
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
async def test_authoritative_original_quote_is_returned_for_funding_action(
    env: Environment,
) -> None:
    original_id = QuoteId(UUID("41f5d73d-a8d3-4d3e-a331-bb9ccdfeb404"))

    async def authoritative(quote: Quote, **_: object) -> Quote:
        return replace(
            quote,
            id=original_id,
            created_at=NOW - timedelta(minutes=2),
            expires_at=NOW + timedelta(minutes=8),
        )

    env.ledger.ensure_quote.side_effect = authoritative
    env.ledger.reserve.return_value = ReservationRejected(
        OPERATION_ID,
        ReservationRejection.INSUFFICIENT_FUNDS,
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert isinstance(outcome, ImageAwaitingFunding)
    assert outcome.quote.id == original_id
    assert outcome.reason is ReservationRejection.INSUFFICIENT_FUNDS
    pricing_input = env.ledger.ensure_quote.await_args.kwargs["pricing_input"]
    assert pricing_input["input_tokens"] == 120
    assert pricing_input["resolution"] == "1K"
    assert len(pricing_input["request_fingerprint"]) == 64
    assert len(pricing_input["delivery_fingerprint"]) == 64
    assert REQUEST.prompt not in repr(pricing_input)
    assert env.invocation.caption not in repr(pricing_input)
    env.image_service.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "reason"),
    [
        (
            Rejected(RejectionReason.INVALID_INPUT),
            ImageNotChargedReason.INVALID_INPUT,
        ),
        (Rejected(RejectionReason.POLICY), ImageNotChargedReason.POLICY_REJECTION),
        (
            Rejected(RejectionReason.UNUSABLE_OUTPUT),
            ImageNotChargedReason.UNUSABLE_OUTPUT,
        ),
        (Failed(FailureReason.PROVIDER_ERROR), ImageNotChargedReason.PROVIDER_FAILURE),
    ],
)
async def test_provider_failure_or_rejection_releases_without_capture(
    env: Environment,
    provider_outcome: object,
    reason: ImageNotChargedReason,
) -> None:
    env.image_service.generate.return_value = provider_outcome

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageNotCharged(OPERATION_ID, reason)
    env.ledger.release.assert_awaited_once()
    env.delivery_service.persist_result.assert_not_awaited()
    env.ledger.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_provider_claim_never_runs_provider_again(
    env: Environment,
) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(state=OperationState.EXECUTING)
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageInProgress(OPERATION_ID, ProgressStage.GENERATING)
    env.ledger.mark_executing.assert_not_awaited()
    env.image_service.generate.assert_not_awaited()
    env.ledger.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_runs_only_when_atomic_claim_changed(
    env: Environment,
) -> None:
    env.ledger.mark_executing.return_value = SettlementResult(
        OPERATION_ID,
        OperationState.EXECUTING,
        changed=False,
    )
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(state=OperationState.EXECUTING)
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageInProgress(OPERATION_ID, ProgressStage.GENERATING)
    env.ledger.mark_executing.assert_awaited_once_with(OPERATION_ID)
    env.image_service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_artifact_resumes_capture_without_provider_rerun(
    env: Environment,
) -> None:
    env.ledger.reserve.return_value = _reservation(idempotent=True)
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.EXECUTING,
            artifact_count=1,
        )
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageDelivered(OPERATION_ID, (501,))
    env.ledger.mark_executing.assert_not_awaited()
    env.image_service.generate.assert_not_awaited()
    env.ledger.capture.assert_awaited_once_with(OPERATION_ID)
    env.delivery_service.mark_ready.assert_awaited_once_with(OPERATION_ID)
    env.delivery_service.deliver.assert_awaited_once_with(OPERATION_ID)


@pytest.mark.asyncio
async def test_restart_uncertainty_reissues_token_without_provider_rerun(
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
    env.delivery_service.deliver.return_value = DeliveryUncertain("network_timeout")

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageDeliveryUncertain(
        OPERATION_ID,
        "network_timeout",
        "reissued-resend-token",
    )
    env.image_service.generate.assert_not_awaited()
    env.delivery_service.mark_ready.assert_not_awaited()
    env.delivery_service.deliver.assert_awaited_once_with(OPERATION_ID)
    env.delivery_service.issue_resend_token.assert_awaited_once_with(OPERATION_ID)


@pytest.mark.asyncio
async def test_initial_delivery_uncertainty_keeps_prepared_resend_token(
    env: Environment,
) -> None:
    env.delivery_service.deliver.return_value = DeliveryUncertain("network_timeout")

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageDeliveryUncertain(
        OPERATION_ID,
        "network_timeout",
        "opaque-resend-token",
    )
    env.delivery_service.issue_resend_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_storage_failure_releases_and_reports_not_charged(
    env: Environment,
) -> None:
    env.delivery_service.persist_result.side_effect = ArtifactStoreError(
        "private store unavailable"
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageNotCharged(
        OPERATION_ID,
        ImageNotChargedReason.RESULT_STORAGE_FAILURE,
    )
    env.ledger.release.assert_awaited_once_with(
        OPERATION_ID,
        reason="image_result_storage_failed",
    )
    env.ledger.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_delivery_failure_surfaces_refund_only_after_reversal(
    env: Environment,
) -> None:
    env.delivery_service.deliver.return_value = DeliveryFailed(
        "TelegramBadRequest",
        retryable=False,
    )
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.REVERSED,
            delivery_state=DeliveryState.FAILED,
            artifact_count=1,
            terminal_reason="delivery_TelegramBadRequest",
        )
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageRefunded(OPERATION_ID, "TelegramBadRequest")
    env.ledger.capture.assert_awaited_once_with(OPERATION_ID)


@pytest.mark.asyncio
async def test_retryable_delivery_failure_remains_in_progress(
    env: Environment,
) -> None:
    env.delivery_service.deliver.return_value = DeliveryFailed(
        "TelegramRetryAfter",
        retryable=True,
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageInProgress(
        OPERATION_ID,
        ProgressStage.DELIVERING,
        "TelegramRetryAfter",
    )


@pytest.mark.asyncio
async def test_terminal_duplicate_maps_existing_reversal_without_provider(
    env: Environment,
) -> None:
    env.ledger.reserve.return_value = ReservationRejected(
        OPERATION_ID,
        ReservationRejection.OPERATION_TERMINAL,
    )
    env.ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            state=OperationState.REVERSED,
            delivery_state=DeliveryState.FAILED,
            terminal_reason="delivery_artifact_expired",
        )
    )

    outcome = await env.coordinator.run(env.invocation, PLAN, REQUEST)

    assert outcome == ImageRefunded(OPERATION_ID, "delivery_artifact_expired")
    env.image_service.generate.assert_not_awaited()
    env.ledger.mark_executing.assert_not_awaited()
