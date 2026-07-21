"""Contracts for two-phase provider-neutral paid text operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from derp.catalog import GoogleModelKey
from derp.execution import (
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.paid_text_operation import (
    PaidTextAwaitingFunding,
    PaidTextDelivered,
    PaidTextInProgress,
    PaidTextInvocation,
    PaidTextNotCharged,
    PaidTextNotChargedReason,
    PaidTextOperationCoordinator,
    PaidTextReadyForDelivery,
    PaidTextRefunded,
)
from derp.features.types import TextOutput
from derp.operations import (
    DeliveryState,
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
    WalletOwner,
    WalletOwnerKind,
)

NOW = datetime(2026, 7, 21, 14, tzinfo=UTC)
REQUESTER_ID = UUID("6b2853d6-f4b8-4ba3-bbe1-e27ff9eafdbf")
CHAT_ID = UUID("98e1b7fe-9dcf-429d-9177-d7b05b58f595")
PLAN = plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING)


def _invocation(
    *,
    input_tokens: int = 8_001,
) -> PaidTextInvocation:
    operation_id = OperationId.for_command(
        feature=Feature.DEEP_THINK,
        chat_id=-100_123,
        message_id=88,
    )
    return PaidTextInvocation(
        operation_id=operation_id,
        request_key=f"deep-think:v1:{operation_id}",
        requester_id=REQUESTER_ID,
        chat_id=CHAT_ID,
        thread_id=17,
        estimated_input_tokens=input_tokens,
        request_binding="b" * 64,
    )


def _quote(invocation: PaidTextInvocation) -> Quote:
    from derp.operations import DeepThinkQuoteInput

    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=invocation.operation_id,
        plan=PLAN,
        quote_input=DeepThinkQuoteInput(invocation.estimated_input_tokens),
        created_at=NOW,
    )


def _reservation(
    invocation: PaidTextInvocation,
    *,
    authorization: FundingAuthorization = FundingAuthorization.CHAT,
    idempotent: bool = False,
) -> ReservedOperation:
    owner = (
        WalletOwner(WalletOwnerKind.CHAT, invocation.chat_id)
        if authorization is FundingAuthorization.CHAT
        else WalletOwner(WalletOwnerKind.USER, invocation.requester_id)
    )
    return ReservedOperation(
        invocation.operation_id,
        InventoryAllocation(owner, allowance_credits=0, purchased_credits=100),
        authorization,
        idempotent,
    )


def _snapshot(
    invocation: PaidTextInvocation,
    *,
    state: OperationState,
    authorization: FundingAuthorization | None = FundingAuthorization.CHAT,
    terminal_reason: str | None = None,
) -> OperationSnapshot:
    timestamp = NOW if state is not OperationState.QUOTED else None
    return OperationSnapshot(
        operation_id=invocation.operation_id,
        quote=_quote(invocation),
        provider_model_id=PLAN.model.provider_model_id,
        request_key=invocation.request_key,
        requester_id=invocation.requester_id,
        chat_id=invocation.chat_id,
        thread_id=invocation.thread_id,
        pricing_input={
            "input_tokens": invocation.estimated_input_tokens,
            "request_binding": invocation.request_binding,
        },
        state=state,
        delivery_state=DeliveryState.NOT_READY,
        wallet_owner=(
            WalletOwner(WalletOwnerKind.CHAT, invocation.chat_id)
            if authorization is not None
            else None
        ),
        funding_authorization=authorization,
        result_metadata={},
        terminal_reason=terminal_reason,
        reserved_at=timestamp,
        execution_started_at=(
            timestamp
            if state
            in {
                OperationState.EXECUTING,
                OperationState.CAPTURED,
                OperationState.RELEASED,
                OperationState.REVERSED,
            }
            else None
        ),
        captured_at=(
            timestamp
            if state in {OperationState.CAPTURED, OperationState.REVERSED}
            else None
        ),
        released_at=timestamp if state is OperationState.RELEASED else None,
        reversed_at=timestamp if state is OperationState.REVERSED else None,
        created_at=NOW,
        updated_at=NOW,
    )


@dataclass(slots=True)
class Environment:
    invocation: PaidTextInvocation
    ledger: MagicMock
    coordinator: PaidTextOperationCoordinator
    executor: AsyncMock


def _environment() -> Environment:
    invocation = _invocation()
    ledger = MagicMock(spec=OperationLedger)
    ledger.ensure_quote = AsyncMock(side_effect=lambda quote, **_: quote)
    ledger.reserve = AsyncMock(return_value=_reservation(invocation))
    ledger.mark_executing = AsyncMock(
        return_value=SettlementResult(
            invocation.operation_id,
            OperationState.EXECUTING,
            True,
        )
    )
    ledger.get_snapshot = AsyncMock()
    ledger.capture = AsyncMock()
    ledger.release = AsyncMock(
        return_value=SettlementResult(
            invocation.operation_id,
            OperationState.RELEASED,
            True,
        )
    )
    executor = AsyncMock(return_value=Succeeded(TextOutput("rigorous answer")))
    return Environment(
        invocation,
        ledger,
        PaidTextOperationCoordinator(ledger, QuoteEngine(), clock=lambda: NOW),
        executor,
    )


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"request_key": " "}, ValueError),
        ({"thread_id": 0}, ValueError),
        ({"estimated_input_tokens": True}, TypeError),
        ({"estimated_input_tokens": -1}, ValueError),
        ({"request_binding": "not-a-binding"}, ValueError),
    ],
)
def test_invocation_rejects_ambiguous_identity_or_pricing_inputs(
    changes: dict[str, object],
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "operation_id": _invocation().operation_id,
        "request_key": "deep-think:v1:test",
        "requester_id": REQUESTER_ID,
        "chat_id": CHAT_ID,
        "thread_id": 17,
        "estimated_input_tokens": 1,
        "request_binding": "b" * 64,
    }
    values.update(changes)

    with pytest.raises(error):
        PaidTextInvocation(**values)  # type: ignore[arg-type]


async def test_success_quotes_claims_and_returns_uncaptured_text() -> None:
    env = _environment()
    request = object()
    events: list[str] = []

    async def reserve(*_args: object, **_kwargs: object) -> ReservedOperation:
        events.append("reserve")
        return _reservation(env.invocation)

    async def mark(*_args: object) -> SettlementResult:
        events.append("claim")
        return SettlementResult(
            env.invocation.operation_id,
            OperationState.EXECUTING,
            True,
        )

    async def execute(*_args: object) -> Succeeded[TextOutput]:
        events.append("provider")
        return Succeeded(TextOutput("rigorous answer"))

    env.ledger.reserve.side_effect = reserve
    env.ledger.mark_executing.side_effect = mark
    env.executor.side_effect = execute

    outcome = await env.coordinator.run(env.invocation, PLAN, request, env.executor)

    assert isinstance(outcome, PaidTextReadyForDelivery)
    assert outcome.output == TextOutput("rigorous answer")
    assert outcome.authorization is FundingAuthorization.CHAT
    assert outcome.quote.key.feature is Feature.DEEP_THINK
    assert outcome.quote.key.model_key is GoogleModelKey.CHAT_REASONING
    assert outcome.quote.key.context_band.value == "medium"
    assert events == ["reserve", "claim", "provider"]
    env.executor.assert_awaited_once_with(PLAN, request)
    env.ledger.capture.assert_not_awaited()
    env.ledger.release.assert_not_awaited()
    env.ledger.reserve.assert_awaited_once_with(
        env.invocation.operation_id,
        allow_personal_once=False,
    )
    ensure_kwargs = env.ledger.ensure_quote.await_args.kwargs
    assert ensure_kwargs == {
        "request_key": env.invocation.request_key,
        "requester_id": env.invocation.requester_id,
        "chat_id": env.invocation.chat_id,
        "thread_id": env.invocation.thread_id,
        "pricing_input": {
            "input_tokens": 8_001,
            "request_binding": "b" * 64,
        },
    }
    assert "rigorous answer" not in repr(outcome)


async def test_capture_delivered_is_explicit_and_idempotent() -> None:
    env = _environment()
    env.ledger.get_snapshot.side_effect = [
        _snapshot(env.invocation, state=OperationState.EXECUTING),
        _snapshot(env.invocation, state=OperationState.CAPTURED),
    ]
    env.ledger.capture.side_effect = [
        SettlementResult(
            env.invocation.operation_id,
            OperationState.CAPTURED,
            True,
        ),
        SettlementResult(
            env.invocation.operation_id,
            OperationState.CAPTURED,
            False,
        ),
    ]

    first = await env.coordinator.capture_delivered(env.invocation.operation_id)
    duplicate = await env.coordinator.capture_delivered(env.invocation.operation_id)

    assert first.changed and not duplicate.changed
    assert first.state is duplicate.state is OperationState.CAPTURED
    assert env.ledger.capture.await_count == 2


async def test_delivery_failure_release_is_explicit_and_idempotent() -> None:
    env = _environment()
    env.ledger.get_snapshot.side_effect = [
        _snapshot(env.invocation, state=OperationState.EXECUTING),
        _snapshot(env.invocation, state=OperationState.RELEASED),
    ]
    env.ledger.release.side_effect = [
        SettlementResult(
            env.invocation.operation_id,
            OperationState.RELEASED,
            True,
        ),
        SettlementResult(
            env.invocation.operation_id,
            OperationState.RELEASED,
            False,
        ),
    ]

    first = await env.coordinator.release_delivery_failure(env.invocation.operation_id)
    duplicate = await env.coordinator.release_delivery_failure(
        env.invocation.operation_id
    )

    assert first.changed and not duplicate.changed
    assert first.state is duplicate.state is OperationState.RELEASED
    assert env.ledger.release.await_args_list[0].kwargs == {
        "reason": "deep_think_delivery_failure"
    }


@pytest.mark.parametrize(
    "reason",
    [
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
        ReservationRejection.INSUFFICIENT_FUNDS,
        ReservationRejection.WALLET_IN_DEBT,
    ],
)
async def test_funding_rejection_keeps_quote_awaiting_explicit_action(
    reason: ReservationRejection,
) -> None:
    env = _environment()
    env.ledger.reserve.return_value = ReservationRejected(
        env.invocation.operation_id,
        reason,
    )

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert isinstance(outcome, PaidTextAwaitingFunding)
    assert outcome.reason is reason
    env.ledger.mark_executing.assert_not_awaited()
    env.executor.assert_not_awaited()
    env.ledger.release.assert_not_awaited()


async def test_expired_quote_is_not_charged_and_never_executes() -> None:
    env = _environment()
    env.ledger.reserve.return_value = ReservationRejected(
        env.invocation.operation_id,
        ReservationRejection.QUOTE_EXPIRED,
    )

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert outcome == PaidTextNotCharged(
        env.invocation.operation_id,
        PaidTextNotChargedReason.QUOTE_EXPIRED,
    )
    env.executor.assert_not_awaited()


@pytest.mark.parametrize(
    ("rejection", "reason"),
    [
        (RejectionReason.INVALID_INPUT, PaidTextNotChargedReason.INVALID_INPUT),
        (RejectionReason.POLICY, PaidTextNotChargedReason.POLICY_REJECTION),
        (RejectionReason.UNUSABLE_OUTPUT, PaidTextNotChargedReason.UNUSABLE_OUTPUT),
    ],
)
async def test_provider_rejection_releases_without_capture(
    rejection: RejectionReason,
    reason: PaidTextNotChargedReason,
) -> None:
    env = _environment()
    env.executor.return_value = Rejected(rejection)

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert outcome == PaidTextNotCharged(env.invocation.operation_id, reason)
    env.ledger.release.assert_awaited_once_with(
        env.invocation.operation_id,
        reason=f"deep_think_{rejection.value}",
    )
    env.ledger.capture.assert_not_awaited()


@pytest.mark.parametrize(
    "provider_outcome",
    [
        Failed(FailureReason.PROVIDER_ERROR),
        Succeeded(object()),
        object(),
    ],
)
async def test_failed_or_malformed_provider_output_releases(
    provider_outcome: object,
) -> None:
    env = _environment()
    env.executor.return_value = provider_outcome

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert isinstance(outcome, PaidTextNotCharged)
    assert outcome.reason in {
        PaidTextNotChargedReason.PROVIDER_FAILURE,
        PaidTextNotChargedReason.UNUSABLE_OUTPUT,
    }
    env.ledger.release.assert_awaited_once()
    env.ledger.capture.assert_not_awaited()


async def test_provider_exception_is_privacy_safely_reported_and_released() -> None:
    env = _environment()
    private_error = RuntimeError("secret request fragment")
    env.executor.side_effect = private_error

    with patch("derp.features.paid_text_operation.report_exception") as report:
        outcome = await env.coordinator.run(
            env.invocation,
            PLAN,
            object(),
            env.executor,
        )

    assert outcome == PaidTextNotCharged(
        env.invocation.operation_id,
        PaidTextNotChargedReason.PROVIDER_FAILURE,
    )
    assert report.call_args.kwargs["exception"] is private_error
    assert set(report.call_args.kwargs) == {
        "exception",
        "level",
        "operation_id",
        "feature",
    }
    env.ledger.release.assert_awaited_once_with(
        env.invocation.operation_id,
        reason="deep_think_provider_failure",
    )


async def test_duplicate_executing_operation_does_not_rerun_provider() -> None:
    env = _environment()
    env.ledger.reserve.return_value = _reservation(env.invocation, idempotent=True)
    env.ledger.get_snapshot.return_value = _snapshot(
        env.invocation,
        state=OperationState.EXECUTING,
    )

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert outcome == PaidTextInProgress(
        env.invocation.operation_id,
        FundingAuthorization.CHAT,
    )
    env.ledger.mark_executing.assert_not_awaited()
    env.executor.assert_not_awaited()


@pytest.mark.parametrize(
    ("state", "terminal_reason", "outcome_type", "reason"),
    [
        (OperationState.CAPTURED, None, PaidTextDelivered, None),
        (
            OperationState.RELEASED,
            "delivery_failed",
            PaidTextNotCharged,
            PaidTextNotChargedReason.RELEASED,
        ),
        (
            OperationState.CANCELED,
            ReservationRejection.QUOTE_EXPIRED.value,
            PaidTextNotCharged,
            PaidTextNotChargedReason.QUOTE_EXPIRED,
        ),
        (
            OperationState.FAILED,
            "failed",
            PaidTextNotCharged,
            PaidTextNotChargedReason.FAILED,
        ),
        (OperationState.REVERSED, "refunded", PaidTextRefunded, None),
    ],
)
async def test_terminal_duplicate_returns_typed_observation_without_provider(
    state: OperationState,
    terminal_reason: str | None,
    outcome_type: type[object],
    reason: PaidTextNotChargedReason | None,
) -> None:
    env = _environment()
    env.ledger.reserve.return_value = ReservationRejected(
        env.invocation.operation_id,
        ReservationRejection.OPERATION_TERMINAL,
    )
    env.ledger.get_snapshot.return_value = _snapshot(
        env.invocation,
        state=state,
        terminal_reason=terminal_reason,
    )

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert isinstance(outcome, outcome_type)
    if isinstance(outcome, PaidTextNotCharged):
        assert outcome.reason is reason
    env.executor.assert_not_awaited()
    env.ledger.mark_executing.assert_not_awaited()


async def test_execution_claim_race_observes_terminal_state_without_rerun() -> None:
    env = _environment()
    env.ledger.mark_executing.side_effect = InvalidOperationTransitionError("captured")
    env.ledger.get_snapshot.return_value = _snapshot(
        env.invocation,
        state=OperationState.CAPTURED,
    )

    outcome = await env.coordinator.run(
        env.invocation,
        PLAN,
        object(),
        env.executor,
    )

    assert isinstance(outcome, PaidTextDelivered)
    env.executor.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "state"),
    [
        ("capture_delivered", OperationState.RESERVED),
        ("release_delivery_failure", OperationState.CAPTURED),
    ],
)
async def test_delivery_settlement_rejects_semantically_invalid_state(
    method: str,
    state: OperationState,
) -> None:
    env = _environment()
    env.ledger.get_snapshot.return_value = _snapshot(env.invocation, state=state)

    with pytest.raises(InvalidOperationTransitionError):
        await getattr(env.coordinator, method)(env.invocation.operation_id)

    env.ledger.capture.assert_not_awaited()
    env.ledger.release.assert_not_awaited()


async def test_wrong_feature_and_non_callable_executor_fail_before_quote() -> None:
    env = _environment()
    chat_plan = plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD)

    with pytest.raises(ValueError, match="cannot execute paid text reasoning"):
        await env.coordinator.run(
            env.invocation,
            chat_plan,
            object(),
            env.executor,
        )
    with pytest.raises(TypeError, match="executor must be callable"):
        await env.coordinator.run(
            env.invocation,
            PLAN,
            object(),
            None,  # type: ignore[arg-type]
        )

    env.ledger.ensure_quote.assert_not_awaited()


async def test_personal_funding_override_must_be_explicit_boolean() -> None:
    env = _environment()

    with pytest.raises(TypeError, match="allow_personal_once must be a boolean"):
        await env.coordinator.run(
            env.invocation,
            PLAN,
            object(),
            env.executor,
            allow_personal_once=1,  # type: ignore[arg-type]
        )

    env.ledger.ensure_quote.assert_not_awaited()


async def test_telemetry_contains_economics_and_outcome_but_no_text() -> None:
    env = _environment()
    private_text = "private reasoning output"
    env.executor.return_value = Succeeded(TextOutput(private_text))
    recorder = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = recorder

    with patch(
        "derp.features.paid_text_operation.logfire.span",
        return_value=context,
    ):
        outcome = await env.coordinator.run(
            env.invocation,
            PLAN,
            {"problem": "private request"},
            env.executor,
        )

    assert isinstance(outcome, PaidTextReadyForDelivery)
    assert "b" * 64 not in repr(env.invocation)
    quote_attributes = recorder.set_attributes.call_args_list[0].args[0]
    outcome_attributes = recorder.set_attributes.call_args_list[1].args[0]
    assert quote_attributes["derp.operation.capability"] == "deep_think"
    assert quote_attributes["derp.operation.model_key"] == "chat_reasoning"
    assert outcome_attributes == {
        "derp.operation.authorization": "chat",
        "derp.operation.outcome": "ready_for_delivery",
        "derp.operation.terminal_outcome": "none",
    }
    exported = repr(recorder.set_attributes.call_args_list)
    assert private_text not in exported
    assert "private request" not in exported
    pricing_input = env.ledger.ensure_quote.await_args.kwargs["pricing_input"]
    assert pricing_input == {
        "input_tokens": env.invocation.estimated_input_tokens,
        "request_binding": env.invocation.request_binding,
    }


async def test_quote_storage_failure_prevents_reservation_and_provider() -> None:
    env = _environment()
    env.ledger.ensure_quote.side_effect = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await env.coordinator.run(
            env.invocation,
            PLAN,
            object(),
            env.executor,
        )

    env.ledger.reserve.assert_not_awaited()
    env.executor.assert_not_awaited()
