"""Two-phase accounting for provider-neutral paid text operations."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import logfire

from derp.execution import (
    ExecutionPlan,
    Failed,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.types import TextOutput
from derp.observability import report_exception
from derp.operations import (
    DeepThinkQuoteInput,
    FundingAuthorization,
    InvalidOperationTransitionError,
    OperationId,
    OperationLedger,
    OperationSnapshot,
    OperationState,
    Quote,
    QuoteEngine,
    QuoteId,
    ReservationRejected,
    ReservationRejection,
    SettlementResult,
    record_operation_outcome,
    record_operation_quote,
)

_FUNDING_REJECTIONS = frozenset(
    {
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
        ReservationRejection.INSUFFICIENT_FUNDS,
        ReservationRejection.WALLET_IN_DEBT,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_operation_id(value: OperationId) -> None:
    if not isinstance(value, OperationId):
        raise TypeError("operation_id must be an OperationId")


@dataclass(frozen=True, slots=True)
class PaidTextInvocation:
    """Stable ownership, scope, and content-free pricing identity for one run."""

    operation_id: OperationId
    request_key: str
    requester_id: uuid.UUID
    chat_id: uuid.UUID
    thread_id: int | None
    estimated_input_tokens: int

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.request_key, str):
            raise TypeError("request_key must be a string")
        request_key = self.request_key.strip()
        if not request_key:
            raise ValueError("request_key must not be blank")
        if len(request_key) > 255:
            raise ValueError("request_key must be at most 255 characters")
        object.__setattr__(self, "request_key", request_key)
        for name in ("requester_id", "chat_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if self.thread_id is not None:
            if isinstance(self.thread_id, bool) or not isinstance(self.thread_id, int):
                raise TypeError("thread_id must be an integer")
            if self.thread_id <= 0:
                raise ValueError("thread_id must be positive")
        DeepThinkQuoteInput(self.estimated_input_tokens)


class PaidTextExecutor[RequestT](Protocol):
    """Provider-neutral callable used only after an atomic execution claim."""

    async def __call__(
        self,
        plan: ExecutionPlan,
        request: RequestT,
        /,
    ) -> Outcome[TextOutput]: ...


class PaidTextNotChargedReason(StrEnum):
    """Stable terminal reasons available to a thin delivery adapter."""

    QUOTE_EXPIRED = "quote_expired"
    INVALID_INPUT = "invalid_input"
    POLICY_REJECTION = "policy_rejection"
    UNUSABLE_OUTPUT = "unusable_output"
    PROVIDER_FAILURE = "provider_failure"
    RELEASED = "released"
    CANCELED = "canceled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PaidTextAwaitingFunding:
    """The immutable quote remains available for an explicit funding decision."""

    operation_id: OperationId
    quote: Quote
    reason: ReservationRejection

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.quote, Quote):
            raise TypeError("quote must be a Quote")
        if self.quote.operation_id != self.operation_id:
            raise ValueError("quote must belong to the operation")
        if self.reason not in _FUNDING_REJECTIONS:
            raise ValueError("awaiting funding requires an actionable funding reason")


@dataclass(frozen=True, slots=True)
class PaidTextReadyForDelivery:
    """Provider text is ready, while spend remains reserved and uncaptured."""

    operation_id: OperationId
    quote: Quote
    authorization: FundingAuthorization
    output: TextOutput = field(repr=False)

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.quote, Quote):
            raise TypeError("quote must be a Quote")
        if self.quote.operation_id != self.operation_id:
            raise ValueError("quote must belong to the operation")
        if not isinstance(self.authorization, FundingAuthorization):
            raise TypeError("authorization must be a FundingAuthorization")
        if not isinstance(self.output, TextOutput):
            raise TypeError("output must be a TextOutput")


@dataclass(frozen=True, slots=True)
class PaidTextInProgress:
    """Another caller owns provider execution or the pending delivery result."""

    operation_id: OperationId
    authorization: FundingAuthorization

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.authorization, FundingAuthorization):
            raise TypeError("authorization must be a FundingAuthorization")


@dataclass(frozen=True, slots=True)
class PaidTextDelivered:
    """A retry observed spend captured only after acknowledged delivery."""

    operation_id: OperationId

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)


@dataclass(frozen=True, slots=True)
class PaidTextNotCharged:
    """The operation ended without captured spend."""

    operation_id: OperationId
    reason: PaidTextNotChargedReason

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.reason, PaidTextNotChargedReason):
            raise TypeError("reason must be a PaidTextNotChargedReason")


@dataclass(frozen=True, slots=True)
class PaidTextRefunded:
    """A retry observed captured spend already reversed by another boundary."""

    operation_id: OperationId

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)


type PaidTextOperationOutcome = (
    PaidTextAwaitingFunding
    | PaidTextReadyForDelivery
    | PaidTextInProgress
    | PaidTextDelivered
    | PaidTextNotCharged
    | PaidTextRefunded
)


@dataclass(frozen=True, slots=True)
class PaidTextSettlement:
    """Typed result of one idempotent post-delivery accounting transition."""

    operation_id: OperationId
    state: OperationState
    changed: bool

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if self.state not in {OperationState.CAPTURED, OperationState.RELEASED}:
            raise ValueError("paid text settlement must capture or release spend")
        if not isinstance(self.changed, bool):
            raise TypeError("changed must be a boolean")


class PaidTextOperationCoordinator:
    """Quote, execute, and expose delivery settlement as separate phases."""

    def __init__(
        self,
        ledger: OperationLedger,
        quote_engine: QuoteEngine,
        *,
        clock: Callable[[], datetime] = _utc_now,
        quote_id_factory: Callable[[], QuoteId] = QuoteId.new,
    ) -> None:
        self._ledger = ledger
        self._quote_engine = quote_engine
        self._clock = clock
        self._quote_id_factory = quote_id_factory

    async def run[RequestT](
        self,
        invocation: PaidTextInvocation,
        plan: ExecutionPlan,
        request: RequestT,
        executor: PaidTextExecutor[RequestT],
        *,
        allow_personal_once: bool = False,
    ) -> PaidTextOperationOutcome:
        """Run provider work once and leave successful spend uncaptured for delivery."""
        self._require_inputs(invocation, plan)
        if not callable(executor):
            raise TypeError("executor must be callable")
        if not isinstance(allow_personal_once, bool):
            raise TypeError("allow_personal_once must be a boolean")
        with logfire.span(
            "paid_text.operation",
            operation_id=str(invocation.operation_id),
            feature=plan.feature.value,
        ) as span:
            quote = await self.ensure_quote(invocation, plan)
            record_operation_quote(
                span,
                quote,
                provider_model_id=plan.model.provider_model_id,
            )
            reservation = await self._ledger.reserve(
                invocation.operation_id,
                allow_personal_once=allow_personal_once,
            )
            if isinstance(reservation, ReservationRejected):
                return self._record_outcome(
                    span,
                    await self._reservation_rejected(reservation, quote),
                )

            authorization = reservation.authorization
            if reservation.idempotent:
                snapshot = await self._text_snapshot(invocation.operation_id)
                if not snapshot.can_claim_execution:
                    return self._record_outcome(
                        span,
                        self._resume(snapshot),
                        authorization=snapshot.funding_authorization,
                    )

            try:
                claim = await self._ledger.mark_executing(invocation.operation_id)
            except InvalidOperationTransitionError:
                snapshot = await self._text_snapshot(invocation.operation_id)
                return self._record_outcome(
                    span,
                    self._resume(snapshot),
                    authorization=snapshot.funding_authorization,
                )
            if not claim.execution_claimed:
                snapshot = await self._text_snapshot(invocation.operation_id)
                return self._record_outcome(
                    span,
                    self._resume(snapshot),
                    authorization=snapshot.funding_authorization,
                )

            outcome = await self._execute(
                invocation.operation_id,
                plan,
                request,
                executor,
            )
            if isinstance(outcome, PaidTextNotCharged):
                return self._record_outcome(
                    span,
                    outcome,
                    authorization=authorization,
                )
            return self._record_outcome(
                span,
                PaidTextReadyForDelivery(
                    invocation.operation_id,
                    quote,
                    authorization,
                    outcome,
                ),
                authorization=authorization,
            )

    async def ensure_quote(
        self,
        invocation: PaidTextInvocation,
        plan: ExecutionPlan,
    ) -> Quote:
        """Persist or recover the exact immutable deep-reasoning quote."""
        self._require_inputs(invocation, plan)
        proposed = self._quote_engine.quote(
            quote_id=self._quote_id_factory(),
            operation_id=invocation.operation_id,
            plan=plan,
            quote_input=DeepThinkQuoteInput(invocation.estimated_input_tokens),
            created_at=self._aware_now(),
        )
        return await self._ledger.ensure_quote(
            proposed,
            request_key=invocation.request_key,
            requester_id=invocation.requester_id,
            chat_id=invocation.chat_id,
            thread_id=invocation.thread_id,
            pricing_input={
                "input_tokens": invocation.estimated_input_tokens,
            },
        )

    async def capture_delivered(
        self,
        operation_id: OperationId,
    ) -> PaidTextSettlement:
        """Capture spend once, only after the adapter confirms delivery."""
        snapshot = await self._text_snapshot(operation_id)
        self._require_state(
            snapshot,
            allowed={OperationState.EXECUTING, OperationState.CAPTURED},
            transition="capture delivered text",
        )
        with logfire.span(
            "paid_text.settlement",
            operation_id=str(operation_id),
        ) as span:
            record_operation_quote(
                span,
                snapshot.quote,
                provider_model_id=snapshot.provider_model_id,
            )
            result = await self._ledger.capture(operation_id)
            record_operation_outcome(
                span,
                "settlement",
                authorization=snapshot.funding_authorization,
                terminal_outcome=result.state.value,
            )
            return self._settlement(result)

    async def release_delivery_failure(
        self,
        operation_id: OperationId,
    ) -> PaidTextSettlement:
        """Release uncaptured spend after delivery definitively fails."""
        snapshot = await self._text_snapshot(operation_id)
        self._require_state(
            snapshot,
            allowed={OperationState.EXECUTING, OperationState.RELEASED},
            transition="release undelivered text",
        )
        with logfire.span(
            "paid_text.settlement",
            operation_id=str(operation_id),
        ) as span:
            record_operation_quote(
                span,
                snapshot.quote,
                provider_model_id=snapshot.provider_model_id,
            )
            result = await self._ledger.release(
                operation_id,
                reason="deep_think_delivery_failure",
            )
            record_operation_outcome(
                span,
                "settlement",
                authorization=snapshot.funding_authorization,
                terminal_outcome=result.state.value,
            )
            return self._settlement(result)

    async def _execute[RequestT](
        self,
        operation_id: OperationId,
        plan: ExecutionPlan,
        request: RequestT,
        executor: PaidTextExecutor[RequestT],
    ) -> TextOutput | PaidTextNotCharged:
        try:
            outcome = await executor(plan, request)
        except Exception as exc:
            report_exception(
                "paid_text_provider_failed",
                exception=exc,
                level="warning",
                operation_id=str(operation_id),
                feature=plan.feature.value,
            )
            await self._release_provider(operation_id, "provider_failure")
            return PaidTextNotCharged(
                operation_id,
                PaidTextNotChargedReason.PROVIDER_FAILURE,
            )

        if isinstance(outcome, Rejected):
            reason = self._rejection_reason(outcome.reason)
            await self._release_provider(operation_id, outcome.reason.value)
            return PaidTextNotCharged(operation_id, reason)
        if isinstance(outcome, Failed):
            await self._release_provider(operation_id, outcome.reason.value)
            return PaidTextNotCharged(
                operation_id,
                PaidTextNotChargedReason.PROVIDER_FAILURE,
            )
        if not isinstance(outcome, Succeeded) or not isinstance(
            outcome.value, TextOutput
        ):
            await self._release_provider(operation_id, "unusable_output")
            return PaidTextNotCharged(
                operation_id,
                PaidTextNotChargedReason.UNUSABLE_OUTPUT,
            )
        return outcome.value

    async def _reservation_rejected(
        self,
        rejection: ReservationRejected,
        quote: Quote,
    ) -> PaidTextOperationOutcome:
        if rejection.reason in _FUNDING_REJECTIONS:
            return PaidTextAwaitingFunding(
                rejection.operation_id,
                quote,
                rejection.reason,
            )
        if rejection.reason is ReservationRejection.QUOTE_EXPIRED:
            return PaidTextNotCharged(
                rejection.operation_id,
                PaidTextNotChargedReason.QUOTE_EXPIRED,
            )
        return self._resume(await self._text_snapshot(rejection.operation_id))

    def _resume(self, snapshot: OperationSnapshot) -> PaidTextOperationOutcome:
        operation_id = snapshot.operation_id
        if snapshot.state in {OperationState.RESERVED, OperationState.EXECUTING}:
            if snapshot.funding_authorization is None:
                raise RuntimeError("in-progress text operation has no authorization")
            return PaidTextInProgress(operation_id, snapshot.funding_authorization)
        if snapshot.state is OperationState.CAPTURED:
            return PaidTextDelivered(operation_id)
        if snapshot.state is OperationState.REVERSED:
            return PaidTextRefunded(operation_id)
        if snapshot.state is OperationState.RELEASED:
            return PaidTextNotCharged(
                operation_id,
                PaidTextNotChargedReason.RELEASED,
            )
        if snapshot.state is OperationState.CANCELED:
            reason = (
                PaidTextNotChargedReason.QUOTE_EXPIRED
                if snapshot.terminal_reason == ReservationRejection.QUOTE_EXPIRED.value
                else PaidTextNotChargedReason.CANCELED
            )
            return PaidTextNotCharged(operation_id, reason)
        if snapshot.state is OperationState.FAILED:
            return PaidTextNotCharged(
                operation_id,
                PaidTextNotChargedReason.FAILED,
            )
        raise RuntimeError(f"text operation remained in unexpected {snapshot.state}")

    async def _text_snapshot(self, operation_id: OperationId) -> OperationSnapshot:
        _require_operation_id(operation_id)
        snapshot = await self._ledger.get_snapshot(operation_id)
        if snapshot.quote.key.feature is not Feature.DEEP_THINK:
            raise ValueError("operation is not a paid deep-reasoning operation")
        return snapshot

    async def _release_provider(self, operation_id: OperationId, reason: str) -> None:
        await self._ledger.release(
            operation_id,
            reason=f"deep_think_{reason}",
        )

    @staticmethod
    def _require_inputs(
        invocation: PaidTextInvocation,
        plan: ExecutionPlan,
    ) -> None:
        if not isinstance(invocation, PaidTextInvocation):
            raise TypeError("invocation must be a PaidTextInvocation")
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be an ExecutionPlan")
        if plan.feature is not Feature.DEEP_THINK:
            raise ValueError(f"{plan.feature.value} cannot execute paid text reasoning")

    @staticmethod
    def _require_state(
        snapshot: OperationSnapshot,
        *,
        allowed: set[OperationState],
        transition: str,
    ) -> None:
        if snapshot.state not in allowed:
            raise InvalidOperationTransitionError(
                f"Cannot {transition} in {snapshot.state.value}"
            )

    @staticmethod
    def _rejection_reason(reason: RejectionReason) -> PaidTextNotChargedReason:
        return {
            RejectionReason.INVALID_INPUT: PaidTextNotChargedReason.INVALID_INPUT,
            RejectionReason.POLICY: PaidTextNotChargedReason.POLICY_REJECTION,
            RejectionReason.UNUSABLE_OUTPUT: PaidTextNotChargedReason.UNUSABLE_OUTPUT,
        }[reason]

    @staticmethod
    def _settlement(result: SettlementResult) -> PaidTextSettlement:
        return PaidTextSettlement(result.operation_id, result.state, result.changed)

    @staticmethod
    def _record_outcome(
        span: logfire.LogfireSpan,
        outcome: PaidTextOperationOutcome,
        *,
        authorization: FundingAuthorization | None = None,
    ) -> PaidTextOperationOutcome:
        terminal_outcome = None
        if isinstance(outcome, PaidTextReadyForDelivery):
            category = "ready_for_delivery"
            authorization = outcome.authorization
        elif isinstance(outcome, PaidTextAwaitingFunding):
            category = f"awaiting_funding:{outcome.reason.value}"
        elif isinstance(outcome, PaidTextInProgress):
            category = "in_progress"
            authorization = outcome.authorization
        elif isinstance(outcome, PaidTextDelivered):
            category = "delivered"
            terminal_outcome = OperationState.CAPTURED.value
        elif isinstance(outcome, PaidTextNotCharged):
            category = f"not_charged:{outcome.reason.value}"
            terminal_outcome = (
                OperationState.CANCELED.value
                if outcome.reason
                in {
                    PaidTextNotChargedReason.QUOTE_EXPIRED,
                    PaidTextNotChargedReason.CANCELED,
                }
                else OperationState.FAILED.value
                if outcome.reason is PaidTextNotChargedReason.FAILED
                else OperationState.RELEASED.value
            )
        elif isinstance(outcome, PaidTextRefunded):
            category = "refunded"
            terminal_outcome = OperationState.REVERSED.value
        else:  # pragma: no cover - guarded by the closed outcome union
            raise TypeError(f"unsupported paid text outcome: {type(outcome).__name__}")
        record_operation_outcome(
            span,
            category,
            authorization=authorization,
            terminal_outcome=terminal_outcome,
        )
        return outcome

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("paid text operation clock must be timezone-aware")
        return now


__all__ = [
    "PaidTextAwaitingFunding",
    "PaidTextDelivered",
    "PaidTextExecutor",
    "PaidTextInProgress",
    "PaidTextInvocation",
    "PaidTextNotCharged",
    "PaidTextNotChargedReason",
    "PaidTextOperationCoordinator",
    "PaidTextOperationOutcome",
    "PaidTextReadyForDelivery",
    "PaidTextRefunded",
    "PaidTextSettlement",
]
