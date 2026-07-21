"""One-shot accounting decisions for ordinary chat turns."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import logfire

from derp.catalog import GoogleModelKey
from derp.execution import ExecutionPlan, Feature, plan_execution
from derp.operations import (
    ChatQuoteInput,
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
    ReservedOperation,
    SettlementResult,
    record_operation_outcome,
    record_operation_quote,
)

PAID_CHAT_PLAN: Final = plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD)
ECONOMY_CHAT_PLAN: Final = plan_execution(Feature.CHAT, GoogleModelKey.CHAT_ECONOMY)

_FUNDING_REJECTIONS: Final = frozenset(
    {
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
        ReservationRejection.INSUFFICIENT_FUNDS,
        ReservationRejection.WALLET_IN_DEBT,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_standard_quote(operation_id: OperationId, quote: Quote) -> None:
    if not isinstance(operation_id, OperationId):
        raise TypeError("operation_id must be an OperationId")
    if not isinstance(quote, Quote):
        raise TypeError("quote must be a Quote")
    if quote.operation_id != operation_id:
        raise ValueError("quote must belong to the chat operation")
    if (
        quote.key.feature is not Feature.CHAT
        or quote.key.model_key is not GoogleModelKey.CHAT_STANDARD
    ):
        raise ValueError("chat accounting requires a standard chat quote")


@dataclass(frozen=True, slots=True)
class ChatTurnInvocation:
    """Stable identity, ownership, scope, and pricing input for one message."""

    telegram_chat_id: int
    telegram_message_id: int
    requester_id: uuid.UUID
    chat_id: uuid.UUID
    thread_id: int | None
    estimated_input_tokens: int

    def __post_init__(self) -> None:
        if isinstance(self.telegram_chat_id, bool) or not isinstance(
            self.telegram_chat_id, int
        ):
            raise TypeError("telegram_chat_id must be an integer")
        if self.telegram_chat_id == 0:
            raise ValueError("telegram_chat_id must not be zero")
        if isinstance(self.telegram_message_id, bool) or not isinstance(
            self.telegram_message_id, int
        ):
            raise TypeError("telegram_message_id must be an integer")
        if self.telegram_message_id <= 0:
            raise ValueError("telegram_message_id must be positive")
        for name in ("requester_id", "chat_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if self.thread_id is not None:
            if isinstance(self.thread_id, bool) or not isinstance(self.thread_id, int):
                raise TypeError("thread_id must be an integer")
            if self.thread_id <= 0:
                raise ValueError("thread_id must be positive")
        ChatQuoteInput(self.estimated_input_tokens)

    @property
    def operation_id(self) -> OperationId:
        """Return the deterministic paid-operation identity for this message."""
        return OperationId.for_command(
            feature=Feature.CHAT,
            chat_id=self.telegram_chat_id,
            message_id=self.telegram_message_id,
        )

    @property
    def request_key(self) -> str:
        """Return a stable, content-free quote idempotency key."""
        return f"chat-turn:v1:{self.operation_id}"


@dataclass(frozen=True, slots=True)
class PaidChatExecutionGrant:
    """The caller owns the only permission to run the standard chat provider."""

    operation_id: OperationId
    quote: Quote
    plan: ExecutionPlan
    authorization: FundingAuthorization

    def __post_init__(self) -> None:
        _require_standard_quote(self.operation_id, self.quote)
        if self.plan != PAID_CHAT_PLAN:
            raise ValueError("paid chat execution requires the standard chat plan")
        if not isinstance(self.authorization, FundingAuthorization):
            raise TypeError("authorization must be a FundingAuthorization")


@dataclass(frozen=True, slots=True)
class EconomyChatExecutionGrant:
    """The caller owns the only permission to run the free economy fallback."""

    operation_id: OperationId
    quote: Quote
    plan: ExecutionPlan
    rejection: ReservationRejection

    def __post_init__(self) -> None:
        _require_standard_quote(self.operation_id, self.quote)
        if self.plan != ECONOMY_CHAT_PLAN:
            raise ValueError("economy chat execution requires the economy chat plan")
        if self.rejection not in _FUNDING_REJECTIONS:
            raise ValueError("economy fallback requires a funding rejection")


@dataclass(frozen=True, slots=True)
class ChatExecutionInProgress:
    """Another caller already owns provider execution for this chat turn."""

    operation_id: OperationId
    quote: Quote
    authorization: FundingAuthorization

    def __post_init__(self) -> None:
        _require_standard_quote(self.operation_id, self.quote)
        if not isinstance(self.authorization, FundingAuthorization):
            raise TypeError("authorization must be a FundingAuthorization")


_ALREADY_HANDLED_STATES: Final = frozenset(
    {
        OperationState.CAPTURED,
        OperationState.RELEASED,
        OperationState.REVERSED,
        OperationState.CANCELED,
        OperationState.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class ChatExecutionAlreadyHandled:
    """A retry observed a state that must never authorize provider work again."""

    operation_id: OperationId
    quote: Quote
    state: OperationState
    authorization: FundingAuthorization | None

    def __post_init__(self) -> None:
        _require_standard_quote(self.operation_id, self.quote)
        if self.state not in _ALREADY_HANDLED_STATES:
            raise ValueError("already-handled state must forbid provider execution")
        if self.authorization is not None and not isinstance(
            self.authorization, FundingAuthorization
        ):
            raise TypeError("authorization must be a FundingAuthorization or None")


type ChatTurnDecision = (
    PaidChatExecutionGrant
    | EconomyChatExecutionGrant
    | ChatExecutionInProgress
    | ChatExecutionAlreadyHandled
)


@dataclass(frozen=True, slots=True)
class ChatTurnSettlement:
    """Typed result of an idempotent post-provider accounting transition."""

    operation_id: OperationId
    state: OperationState
    changed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, OperationId):
            raise TypeError("operation_id must be an OperationId")
        if not isinstance(self.changed, bool):
            raise TypeError("changed must be a boolean")
        if self.state not in {
            OperationState.CAPTURED,
            OperationState.RELEASED,
            OperationState.REVERSED,
        }:
            raise ValueError("chat settlement state is not externally settleable")


class ChatTurnAccounting:
    """Quote, authorize, and settle one ordinary chat turn without provider I/O."""

    def __init__(
        self,
        ledger: OperationLedger,
        quote_engine: QuoteEngine | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        quote_id_factory: Callable[[], QuoteId] = QuoteId.new,
    ) -> None:
        self._ledger = ledger
        self._quote_engine = quote_engine or QuoteEngine()
        self._clock = clock
        self._quote_id_factory = quote_id_factory

    async def authorize(self, invocation: ChatTurnInvocation) -> ChatTurnDecision:
        """Return at most one provider execution grant for one stable message."""
        if not isinstance(invocation, ChatTurnInvocation):
            raise TypeError("invocation must be a ChatTurnInvocation")

        operation_id = invocation.operation_id
        proposed = self._quote_engine.quote(
            quote_id=self._quote_id_factory(),
            operation_id=operation_id,
            plan=PAID_CHAT_PLAN,
            quote_input=ChatQuoteInput(invocation.estimated_input_tokens),
            created_at=self._aware_now(),
        )
        with logfire.span(
            "chat.turn.accounting",
            operation_id=str(operation_id),
        ) as span:
            quote = await self._ledger.ensure_quote(
                proposed,
                request_key=invocation.request_key,
                requester_id=invocation.requester_id,
                chat_id=invocation.chat_id,
                thread_id=invocation.thread_id,
                pricing_input={"input_tokens": invocation.estimated_input_tokens},
            )
            self._set_economics(span, quote)
            reservation = await self._ledger.reserve(
                operation_id,
                allow_personal_once=False,
            )
            if isinstance(reservation, ReservationRejected):
                decision = await self._rejection_decision(reservation, quote)
            else:
                decision = await self._claim_or_observe(reservation, quote)
            self._set_decision(span, decision)
            return decision

    async def capture_success(
        self,
        operation_id: OperationId,
    ) -> ChatTurnSettlement:
        """Capture a successful provider call exactly once."""
        snapshot = await self._chat_snapshot(operation_id)
        self._require_state(
            snapshot,
            allowed={OperationState.EXECUTING, OperationState.CAPTURED},
            transition="capture successful chat execution",
        )
        with logfire.span(
            "chat.turn.settlement",
            operation_id=str(operation_id),
        ) as span:
            self._set_economics(span, snapshot.quote)
            result = await self._ledger.capture(operation_id)
            record_operation_outcome(
                span,
                "settlement",
                authorization=snapshot.funding_authorization,
                terminal_outcome=result.state.value,
            )
            return self._settlement(result)

    async def release_provider_failure(
        self,
        operation_id: OperationId,
    ) -> ChatTurnSettlement:
        """Release a reservation after provider termination is definitive."""
        return await self._release_uncaptured(
            operation_id,
            transition="release failed chat execution",
            reason="chat_provider_definitive_failure",
        )

    async def release_delivery_failure(
        self,
        operation_id: OperationId,
    ) -> ChatTurnSettlement:
        """Release uncaptured spend when model content was not delivered."""
        return await self._release_uncaptured(
            operation_id,
            transition="release undelivered chat execution",
            reason="chat_delivery_definitive_failure",
        )

    async def _release_uncaptured(
        self,
        operation_id: OperationId,
        *,
        transition: str,
        reason: str,
    ) -> ChatTurnSettlement:
        """Apply one idempotent release before successful delivery capture."""
        snapshot = await self._chat_snapshot(operation_id)
        self._require_state(
            snapshot,
            allowed={OperationState.EXECUTING, OperationState.RELEASED},
            transition=transition,
        )
        with logfire.span(
            "chat.turn.settlement",
            operation_id=str(operation_id),
        ) as span:
            self._set_economics(span, snapshot.quote)
            result = await self._ledger.release(
                operation_id,
                reason=reason,
            )
            record_operation_outcome(
                span,
                "settlement",
                authorization=snapshot.funding_authorization,
                terminal_outcome=result.state.value,
            )
            return self._settlement(result)

    async def reverse_delivery_failure(
        self,
        operation_id: OperationId,
    ) -> ChatTurnSettlement:
        """Reverse captured chat spend after a definitive delivery failure."""
        snapshot = await self._chat_snapshot(operation_id)
        self._require_state(
            snapshot,
            allowed={OperationState.CAPTURED, OperationState.REVERSED},
            transition="reverse failed chat delivery",
        )
        with logfire.span(
            "chat.turn.settlement",
            operation_id=str(operation_id),
        ) as span:
            self._set_economics(span, snapshot.quote)
            result = await self._ledger.reverse(
                operation_id,
                reason="chat_delivery_failure",
            )
            record_operation_outcome(
                span,
                "settlement",
                authorization=snapshot.funding_authorization,
                terminal_outcome=result.state.value,
            )
            return self._settlement(result)

    async def _rejection_decision(
        self,
        rejection: ReservationRejected,
        quote: Quote,
    ) -> ChatTurnDecision:
        if rejection.reason not in _FUNDING_REJECTIONS:
            return await self._observe(rejection.operation_id)
        try:
            canceled = await self._ledger.cancel(
                rejection.operation_id,
                reason=f"chat_economy_fallback:{rejection.reason.value}",
            )
        except InvalidOperationTransitionError:
            return await self._observe(rejection.operation_id)
        if canceled.changed:
            return EconomyChatExecutionGrant(
                rejection.operation_id,
                quote,
                ECONOMY_CHAT_PLAN,
                rejection.reason,
            )
        return await self._observe(rejection.operation_id)

    async def _claim_or_observe(
        self,
        reservation: ReservedOperation,
        quote: Quote,
    ) -> ChatTurnDecision:
        try:
            claim = await self._ledger.mark_executing(reservation.operation_id)
        except InvalidOperationTransitionError:
            return await self._observe(reservation.operation_id)
        if claim.execution_claimed:
            return PaidChatExecutionGrant(
                reservation.operation_id,
                quote,
                PAID_CHAT_PLAN,
                reservation.authorization,
            )
        return await self._observe(reservation.operation_id)

    async def _observe(self, operation_id: OperationId) -> ChatTurnDecision:
        for _ in range(2):
            snapshot = await self._chat_snapshot(operation_id)
            if snapshot.state is OperationState.RESERVED:
                if snapshot.funding_authorization is None:
                    raise RuntimeError("reserved chat operation has no authorization")
                try:
                    claim = await self._ledger.mark_executing(operation_id)
                except InvalidOperationTransitionError:
                    continue
                if claim.execution_claimed:
                    return PaidChatExecutionGrant(
                        operation_id,
                        snapshot.quote,
                        PAID_CHAT_PLAN,
                        snapshot.funding_authorization,
                    )
                continue
            if snapshot.state is OperationState.EXECUTING:
                if snapshot.funding_authorization is None:
                    raise RuntimeError("executing chat operation has no authorization")
                return ChatExecutionInProgress(
                    operation_id,
                    snapshot.quote,
                    snapshot.funding_authorization,
                )
            if snapshot.state in _ALREADY_HANDLED_STATES:
                return ChatExecutionAlreadyHandled(
                    operation_id,
                    snapshot.quote,
                    snapshot.state,
                    snapshot.funding_authorization,
                )
            raise RuntimeError(
                f"chat operation remained in unexpected {snapshot.state}"
            )
        raise RuntimeError(
            "chat operation changed state repeatedly while being observed"
        )

    async def _chat_snapshot(self, operation_id: OperationId) -> OperationSnapshot:
        if not isinstance(operation_id, OperationId):
            raise TypeError("operation_id must be an OperationId")
        snapshot = await self._ledger.get_snapshot(operation_id)
        quote = snapshot.quote
        if (
            quote.key.feature is not Feature.CHAT
            or quote.key.model_key is not GoogleModelKey.CHAT_STANDARD
        ):
            raise ValueError("operation is not a standard ordinary chat turn")
        return snapshot

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
    def _settlement(result: SettlementResult) -> ChatTurnSettlement:
        return ChatTurnSettlement(result.operation_id, result.state, result.changed)

    @staticmethod
    def _set_economics(span: logfire.LogfireSpan, quote: Quote) -> None:
        record_operation_quote(
            span,
            quote,
            provider_model_id=PAID_CHAT_PLAN.model.provider_model_id,
        )

    @staticmethod
    def _set_decision(
        span: logfire.LogfireSpan,
        decision: ChatTurnDecision,
    ) -> None:
        if isinstance(decision, PaidChatExecutionGrant):
            authorization = decision.authorization
            outcome = "paid_execution_granted"
        elif isinstance(decision, EconomyChatExecutionGrant):
            authorization = None
            outcome = "economy_execution_granted"
        elif isinstance(decision, ChatExecutionInProgress):
            authorization = decision.authorization
            outcome = "execution_in_progress"
        else:
            authorization = decision.authorization
            outcome = f"already_{decision.state.value}"
        record_operation_outcome(
            span,
            outcome,
            authorization=authorization,
            terminal_outcome=(
                decision.state.value
                if isinstance(decision, ChatExecutionAlreadyHandled)
                else None
            ),
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("chat accounting clock must be timezone-aware")
        return now


__all__ = [
    "ChatExecutionAlreadyHandled",
    "ChatExecutionInProgress",
    "ChatTurnAccounting",
    "ChatTurnDecision",
    "ChatTurnInvocation",
    "ChatTurnSettlement",
    "EconomyChatExecutionGrant",
    "PaidChatExecutionGrant",
]
