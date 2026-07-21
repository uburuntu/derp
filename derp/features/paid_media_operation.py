"""Durable settlement and delivery for provider-backed paid media."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import logfire

from derp.artifacts import ArtifactStoreError
from derp.delivery.types import (
    Delivered,
    DeliveryFailed,
    DeliveryMedia,
    DeliveryTarget,
    DeliveryUncertain,
    ProgressStage,
    TelegramMediaKind,
    classify_delivery_batch,
)
from derp.execution import (
    ExecutionPlan,
    Failed,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.observability import report_exception
from derp.operations import (
    DeliveryState,
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
    TtsQuoteInput,
    VideoGenerateQuoteInput,
    record_operation_outcome,
    record_operation_quote,
)

if TYPE_CHECKING:
    from derp.delivery.service import DeliveryService

type PaidMediaQuoteInput = TtsQuoteInput | VideoGenerateQuoteInput

_PAID_MEDIA_FEATURES = frozenset({Feature.TTS, Feature.VIDEO_GENERATE})
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
class PaidMediaInvocation:
    """Stable identity, ownership, pricing, and destination for one media run."""

    operation_id: OperationId
    request_key: str
    requester_id: uuid.UUID
    chat_id: uuid.UUID
    thread_id: int | None
    target: DeliveryTarget
    estimated_input_tokens: int
    request_binding: str = field(repr=False)

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
        if not isinstance(self.target, DeliveryTarget):
            raise TypeError("target must be a DeliveryTarget")
        if self.thread_id != self.target.thread_id:
            raise ValueError("operation and delivery thread scopes must match")
        if isinstance(self.estimated_input_tokens, bool) or not isinstance(
            self.estimated_input_tokens, int
        ):
            raise TypeError("estimated_input_tokens must be an integer")
        if self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must not be negative")
        if not isinstance(self.request_binding, str):
            raise TypeError("request_binding must be a string")
        if len(self.request_binding) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_binding
        ):
            raise ValueError(
                "request_binding must be a lowercase hexadecimal SHA-256 HMAC"
            )


@dataclass(frozen=True, slots=True)
class PaidMediaResult:
    """Immutable provider output ready for durable Telegram delivery."""

    media: tuple[DeliveryMedia, ...] = field(repr=False)
    caption: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.media, tuple) or not self.media:
            raise ValueError("paid media result must contain an immutable media tuple")
        if any(not isinstance(item, DeliveryMedia) for item in self.media):
            raise TypeError("paid media result items must be DeliveryMedia")
        if self.caption is not None:
            if not isinstance(self.caption, str):
                raise TypeError("caption must be a string")
            caption = self.caption.strip()
            if not caption:
                raise ValueError("caption must not be blank")
            object.__setattr__(self, "caption", caption)


class PaidMediaExecutor[RequestT](Protocol):
    """Provider adapter that has no knowledge of billing or Telegram."""

    async def __call__(
        self,
        plan: ExecutionPlan,
        request: RequestT,
        /,
    ) -> Outcome[PaidMediaResult]: ...


class PaidMediaNotChargedReason(StrEnum):
    """Stable terminal reasons available to thin user-interface adapters."""

    QUOTE_EXPIRED = "quote_expired"
    INVALID_INPUT = "invalid_input"
    POLICY_REJECTION = "policy_rejection"
    UNUSABLE_OUTPUT = "unusable_output"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    RESULT_STORAGE_FAILURE = "result_storage_failure"
    RELEASED = "released"
    CANCELED = "canceled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PaidMediaAwaitingFunding:
    """The immutable quote needs an explicit funding decision."""

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
class PaidMediaInProgress:
    """Existing provider or delivery work must settle before another attempt."""

    operation_id: OperationId
    stage: ProgressStage
    code: str | None = None

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.stage, ProgressStage):
            raise TypeError("stage must be a ProgressStage")
        if self.code is not None:
            if not isinstance(self.code, str):
                raise TypeError("in-progress code must be a string")
            code = self.code.strip()
            if not code:
                raise ValueError("in-progress code must not be blank")
            object.__setattr__(self, "code", code)


@dataclass(frozen=True, slots=True)
class PaidMediaDelivered:
    """Telegram acknowledged the operation's complete persisted result."""

    operation_id: OperationId
    message_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.message_ids, tuple) or not self.message_ids:
            raise ValueError("delivered message IDs must use a non-empty tuple")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.message_ids
        ):
            raise ValueError("delivered message IDs must be positive integers")


@dataclass(frozen=True, slots=True)
class PaidMediaDeliveryUncertain:
    """Telegram may have accepted the result; only authenticated resend may retry."""

    operation_id: OperationId
    code: str
    resend_token: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("delivery uncertainty code must not be blank")
        if not isinstance(self.resend_token, str) or not self.resend_token.strip():
            raise ValueError("resend token must not be blank")


@dataclass(frozen=True, slots=True)
class PaidMediaNotCharged:
    """The operation ended before capture and its reservation was released."""

    operation_id: OperationId
    reason: PaidMediaNotChargedReason

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.reason, PaidMediaNotChargedReason):
            raise TypeError("reason must be a PaidMediaNotChargedReason")


@dataclass(frozen=True, slots=True)
class PaidMediaRefunded:
    """Captured spend was returned after terminal delivery failure."""

    operation_id: OperationId
    code: str

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("refund code must not be blank")


type PaidMediaOperationOutcome = (
    PaidMediaAwaitingFunding
    | PaidMediaInProgress
    | PaidMediaDelivered
    | PaidMediaDeliveryUncertain
    | PaidMediaNotCharged
    | PaidMediaRefunded
)


class PaidMediaOperationCoordinator:
    """Run or safely resume one provider-neutral paid media operation."""

    def __init__(
        self,
        ledger: OperationLedger,
        quote_engine: QuoteEngine,
        delivery_service: DeliveryService,
        *,
        clock: Callable[[], datetime] = _utc_now,
        quote_id_factory: Callable[[], QuoteId] = QuoteId.new,
        provider_timeout: timedelta = timedelta(minutes=10),
    ) -> None:
        if not isinstance(provider_timeout, timedelta):
            raise TypeError("provider_timeout must be a timedelta")
        if provider_timeout <= timedelta(0):
            raise ValueError("provider_timeout must be positive")
        self._ledger = ledger
        self._quote_engine = quote_engine
        self._delivery_service = delivery_service
        self._clock = clock
        self._quote_id_factory = quote_id_factory
        self._provider_timeout_seconds = provider_timeout.total_seconds()

    async def run[RequestT](
        self,
        invocation: PaidMediaInvocation,
        plan: ExecutionPlan,
        quote_input: PaidMediaQuoteInput,
        request: RequestT,
        executor: PaidMediaExecutor[RequestT],
        *,
        allow_personal_once: bool = False,
    ) -> PaidMediaOperationOutcome:
        """Execute only a newly claimed provider call; otherwise resume safely."""
        feature = self._require_matching_feature(invocation, plan, quote_input)
        if not callable(executor):
            raise TypeError("executor must be callable")
        if not isinstance(allow_personal_once, bool):
            raise TypeError("allow_personal_once must be a boolean")

        with logfire.span(
            "paid_media.operation",
            operation_id=str(invocation.operation_id),
            feature=feature.value,
        ) as span:
            quote = await self.ensure_quote(invocation, plan, quote_input)
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
                outcome = await self._reservation_rejected(reservation, quote)
                return self._record_outcome(span, outcome)

            authorization = reservation.authorization
            if reservation.idempotent:
                snapshot = await self._ledger.get_snapshot(invocation.operation_id)
                if not snapshot.can_claim_execution:
                    return self._record_outcome(
                        span,
                        await self._resume(snapshot),
                        authorization=snapshot.funding_authorization,
                    )

            try:
                claim = await self._ledger.mark_executing(invocation.operation_id)
            except InvalidOperationTransitionError:
                snapshot = await self._ledger.get_snapshot(invocation.operation_id)
                return self._record_outcome(
                    span,
                    await self._resume(snapshot),
                    authorization=snapshot.funding_authorization,
                )
            if not claim.execution_claimed:
                snapshot = await self._ledger.get_snapshot(invocation.operation_id)
                return self._record_outcome(
                    span,
                    await self._resume(snapshot),
                    authorization=snapshot.funding_authorization,
                )

            provider_outcome = await self._execute(
                invocation,
                plan,
                request,
                executor,
            )
            if isinstance(provider_outcome, PaidMediaNotCharged):
                return self._record_outcome(
                    span,
                    provider_outcome,
                    authorization=authorization,
                )
            if not isinstance(provider_outcome, Succeeded):
                raise RuntimeError(
                    "paid media execution returned an unsupported outcome"
                )

            result = provider_outcome.value
            if not isinstance(result, PaidMediaResult) or not self._usable_result(
                feature, result
            ):
                await self._release(
                    invocation.operation_id,
                    feature,
                    "unusable_output",
                )
                return self._record_outcome(
                    span,
                    PaidMediaNotCharged(
                        invocation.operation_id,
                        PaidMediaNotChargedReason.UNUSABLE_OUTPUT,
                    ),
                    authorization=authorization,
                )

            try:
                prepared = await self._delivery_service.persist_result(
                    invocation.operation_id,
                    media=result.media,
                    target=invocation.target,
                    caption=result.caption,
                )
            except ArtifactStoreError:
                await self._release(
                    invocation.operation_id,
                    feature,
                    "result_storage_failed",
                )
                return self._record_outcome(
                    span,
                    PaidMediaNotCharged(
                        invocation.operation_id,
                        PaidMediaNotChargedReason.RESULT_STORAGE_FAILURE,
                    ),
                    authorization=authorization,
                )

            if prepared.operation_id != invocation.operation_id:
                raise RuntimeError(
                    "persisted media result belongs to another operation"
                )
            await self._capture_persisted_result(invocation.operation_id)
            try:
                await self._delivery_service.mark_ready(invocation.operation_id)
                delivery_outcome = await self._deliver(
                    invocation.operation_id,
                    resend_token=prepared.resend_token,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delivery_outcome = await self._recover_captured_delivery(
                    invocation.operation_id
                )
                report_exception(
                    "paid_media_delivery_recovery_needed",
                    exception=exc,
                    level="warning",
                    operation_id=str(invocation.operation_id),
                    feature=feature.value,
                )
            return self._record_outcome(
                span,
                delivery_outcome,
                authorization=authorization,
            )

    async def ensure_quote(
        self,
        invocation: PaidMediaInvocation,
        plan: ExecutionPlan,
        quote_input: PaidMediaQuoteInput,
    ) -> Quote:
        """Persist or recover one immutable quote without reserving funds."""
        self._require_matching_feature(invocation, plan, quote_input)
        proposed = self._quote_engine.quote(
            quote_id=self._quote_id_factory(),
            operation_id=invocation.operation_id,
            plan=plan,
            quote_input=quote_input,
            created_at=self._aware_now(),
        )
        return await self._ledger.ensure_quote(
            proposed,
            request_key=invocation.request_key,
            requester_id=invocation.requester_id,
            chat_id=invocation.chat_id,
            thread_id=invocation.thread_id,
            pricing_input=self._pricing_input(invocation, quote_input),
        )

    async def _execute[RequestT](
        self,
        invocation: PaidMediaInvocation,
        plan: ExecutionPlan,
        request: RequestT,
        executor: PaidMediaExecutor[RequestT],
    ) -> Succeeded[PaidMediaResult] | PaidMediaNotCharged:
        try:
            async with asyncio.timeout(self._provider_timeout_seconds):
                outcome = await executor(plan, request)
        except TimeoutError:
            await self._release(
                invocation.operation_id,
                plan.feature,
                "provider_timeout",
            )
            return PaidMediaNotCharged(
                invocation.operation_id,
                PaidMediaNotChargedReason.PROVIDER_TIMEOUT,
            )
        except Exception as exc:
            report_exception(
                "paid_media_provider_failed",
                exception=exc,
                level="warning",
                operation_id=str(invocation.operation_id),
                feature=plan.feature.value,
            )
            await self._release(
                invocation.operation_id,
                plan.feature,
                "provider_failure",
            )
            return PaidMediaNotCharged(
                invocation.operation_id,
                PaidMediaNotChargedReason.PROVIDER_FAILURE,
            )

        if isinstance(outcome, Rejected):
            reason = self._rejection_reason(outcome.reason)
            await self._release(
                invocation.operation_id,
                plan.feature,
                outcome.reason.value,
            )
            return PaidMediaNotCharged(invocation.operation_id, reason)
        if isinstance(outcome, Failed):
            await self._release(
                invocation.operation_id,
                plan.feature,
                outcome.reason.value,
            )
            return PaidMediaNotCharged(
                invocation.operation_id,
                PaidMediaNotChargedReason.PROVIDER_FAILURE,
            )
        if not isinstance(outcome, Succeeded):
            await self._release(
                invocation.operation_id,
                plan.feature,
                "provider_contract_error",
            )
            raise TypeError("executor must return a supported Outcome")
        return outcome

    async def _reservation_rejected(
        self,
        rejection: ReservationRejected,
        quote: Quote,
    ) -> PaidMediaOperationOutcome:
        if rejection.reason in _FUNDING_REJECTIONS:
            return PaidMediaAwaitingFunding(
                rejection.operation_id,
                quote,
                rejection.reason,
            )
        if rejection.reason is ReservationRejection.QUOTE_EXPIRED:
            return PaidMediaNotCharged(
                rejection.operation_id,
                PaidMediaNotChargedReason.QUOTE_EXPIRED,
            )
        return await self._resume_operation(rejection.operation_id)

    async def _resume_operation(
        self,
        operation_id: OperationId,
    ) -> PaidMediaOperationOutcome:
        return await self._resume(await self._ledger.get_snapshot(operation_id))

    async def _resume(
        self,
        snapshot: OperationSnapshot,
    ) -> PaidMediaOperationOutcome:
        operation_id = snapshot.operation_id
        if snapshot.state is OperationState.EXECUTING:
            artifact_count = snapshot.result_metadata.get("artifact_count")
            if (
                isinstance(artifact_count, int)
                and not isinstance(artifact_count, bool)
                and artifact_count > 0
            ):
                settlement = await self._ledger.capture_persisted_result(operation_id)
                if settlement is None:
                    return PaidMediaInProgress(
                        operation_id,
                        ProgressStage.PREPARING,
                        "result_reconciliation_pending",
                    )
                self._require_captured(settlement, operation_id)
                await self._delivery_service.mark_ready(operation_id)
                return await self._deliver(operation_id)
            return PaidMediaInProgress(operation_id, ProgressStage.GENERATING)
        if snapshot.state is OperationState.CAPTURED:
            if snapshot.delivery_state is DeliveryState.NOT_READY:
                await self._delivery_service.mark_ready(operation_id)
            return await self._deliver(operation_id)
        if snapshot.state is OperationState.REVERSED:
            return PaidMediaRefunded(
                operation_id,
                snapshot.terminal_reason or "delivery_failed",
            )
        if snapshot.state is OperationState.RELEASED:
            return PaidMediaNotCharged(
                operation_id,
                PaidMediaNotChargedReason.RELEASED,
            )
        if snapshot.state is OperationState.CANCELED:
            return PaidMediaNotCharged(
                operation_id,
                PaidMediaNotChargedReason.CANCELED,
            )
        if snapshot.state is OperationState.FAILED:
            return PaidMediaNotCharged(
                operation_id,
                PaidMediaNotChargedReason.FAILED,
            )
        return PaidMediaInProgress(operation_id, ProgressStage.PREPARING)

    async def _capture_persisted_result(self, operation_id: OperationId) -> None:
        settlement = await self._ledger.capture_persisted_result(operation_id)
        if settlement is None:
            raise RuntimeError("persisted media result could not be captured")
        self._require_captured(settlement, operation_id)

    async def _deliver(
        self,
        operation_id: OperationId,
        *,
        resend_token: str | None = None,
    ) -> PaidMediaOperationOutcome:
        outcome = await self._delivery_service.deliver(operation_id)
        if isinstance(outcome, Delivered):
            return PaidMediaDelivered(operation_id, outcome.message_ids)
        if isinstance(outcome, DeliveryUncertain):
            if outcome.code == "attempt_in_progress_or_interrupted":
                return PaidMediaInProgress(
                    operation_id,
                    ProgressStage.DELIVERING,
                    outcome.code,
                )
            if resend_token is None:
                resend_token = await self._delivery_service.issue_resend_token(
                    operation_id
                )
            return PaidMediaDeliveryUncertain(
                operation_id,
                outcome.code,
                resend_token,
            )
        if not isinstance(outcome, DeliveryFailed):
            raise TypeError("delivery service returned an unsupported outcome")
        if outcome.retryable:
            return PaidMediaInProgress(
                operation_id,
                ProgressStage.DELIVERING,
                outcome.code,
            )
        snapshot = await self._ledger.get_snapshot(operation_id)
        if snapshot.state is not OperationState.REVERSED:
            raise RuntimeError(
                "terminal delivery failure did not reverse captured spend"
            )
        return PaidMediaRefunded(operation_id, outcome.code)

    async def _recover_captured_delivery(
        self,
        operation_id: OperationId,
    ) -> PaidMediaOperationOutcome:
        """Return the durable truth after an exception beyond spend capture."""
        snapshot = await self._ledger.get_snapshot(operation_id)
        if snapshot.state is OperationState.REVERSED:
            return PaidMediaRefunded(
                operation_id,
                snapshot.terminal_reason or "delivery_failed",
            )
        if snapshot.state is not OperationState.CAPTURED:
            raise RuntimeError(
                "captured media operation changed to an unsupported state"
            )

        inspection = await self._delivery_service.inspect(operation_id)
        if inspection.state is DeliveryState.DELIVERED and inspection.message_ids:
            return PaidMediaDelivered(operation_id, inspection.message_ids)
        if inspection.state is DeliveryState.UNCERTAIN:
            try:
                token = await self._delivery_service.issue_resend_token(operation_id)
            except Exception:
                return PaidMediaInProgress(
                    operation_id,
                    ProgressStage.DELIVERING,
                    inspection.last_error_code or "delivery_reconciliation_pending",
                )
            return PaidMediaDeliveryUncertain(
                operation_id,
                inspection.last_error_code or "delivery_uncertain",
                token,
            )
        return PaidMediaInProgress(
            operation_id,
            ProgressStage.DELIVERING,
            inspection.last_error_code or "delivery_reconciliation_pending",
        )

    async def _release(
        self,
        operation_id: OperationId,
        feature: Feature,
        reason: str,
    ) -> None:
        await self._ledger.release(
            operation_id,
            reason=f"{feature.value}_{reason}",
        )

    @staticmethod
    def _require_captured(
        settlement: SettlementResult,
        operation_id: OperationId,
    ) -> None:
        if not isinstance(settlement, SettlementResult):
            raise TypeError("capture must return a SettlementResult")
        if settlement.operation_id != operation_id:
            raise RuntimeError("capture settled a different operation")
        if settlement.state is not OperationState.CAPTURED:
            raise RuntimeError("persisted media result did not reach captured state")

    @staticmethod
    def _usable_result(feature: Feature, result: PaidMediaResult) -> bool:
        if len(result.media) != 1:
            return False
        expected_kinds = (
            {TelegramMediaKind.AUDIO, TelegramMediaKind.VOICE}
            if feature is Feature.TTS
            else {TelegramMediaKind.VIDEO}
        )
        if any(item.kind not in expected_kinds for item in result.media):
            return False
        try:
            classify_delivery_batch(tuple(item.kind for item in result.media))
        except ValueError:
            return False
        return True

    @staticmethod
    def _require_matching_feature(
        invocation: PaidMediaInvocation,
        plan: ExecutionPlan,
        quote_input: PaidMediaQuoteInput,
    ) -> Feature:
        if not isinstance(invocation, PaidMediaInvocation):
            raise TypeError("invocation must be a PaidMediaInvocation")
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be an ExecutionPlan")
        if plan.feature not in _PAID_MEDIA_FEATURES:
            raise ValueError("paid media operations support only TTS and video")
        if isinstance(quote_input, TtsQuoteInput):
            feature = Feature.TTS
        elif isinstance(quote_input, VideoGenerateQuoteInput):
            feature = Feature.VIDEO_GENERATE
        else:
            raise TypeError("quote_input must be a paid media quote input")
        if plan.feature is not feature:
            raise ValueError(
                f"{type(quote_input).__name__} cannot price {plan.feature.value}"
            )
        if invocation.estimated_input_tokens != quote_input.input_tokens:
            raise ValueError("invocation and quote input token estimates must match")
        return feature

    @classmethod
    def _pricing_input(
        cls,
        invocation: PaidMediaInvocation,
        quote_input: PaidMediaQuoteInput,
    ) -> Mapping[str, object]:
        pricing_input: dict[str, object] = {
            "input_tokens": quote_input.input_tokens,
            "request_binding": invocation.request_binding,
            "delivery_fingerprint": cls._delivery_fingerprint(invocation.target),
        }
        if isinstance(quote_input, TtsQuoteInput):
            pricing_input["output_seconds"] = quote_input.output_seconds
        else:
            pricing_input.update(
                {
                    "duration_seconds": quote_input.duration_seconds,
                    "resolution": quote_input.resolution.value,
                }
            )
        return pricing_input

    @staticmethod
    def _delivery_fingerprint(target: DeliveryTarget) -> str:
        encoded = json.dumps(
            {
                "business_connection_id": target.business_connection_id,
                "chat_id": target.chat_id,
                "reply_to_message_id": target.reply_to_message_id,
                "thread_id": target.thread_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _rejection_reason(reason: RejectionReason) -> PaidMediaNotChargedReason:
        return {
            RejectionReason.INVALID_INPUT: PaidMediaNotChargedReason.INVALID_INPUT,
            RejectionReason.POLICY: PaidMediaNotChargedReason.POLICY_REJECTION,
            RejectionReason.UNUSABLE_OUTPUT: PaidMediaNotChargedReason.UNUSABLE_OUTPUT,
        }[reason]

    @staticmethod
    def _record_outcome(
        span: logfire.LogfireSpan,
        outcome: PaidMediaOperationOutcome,
        *,
        authorization: FundingAuthorization | None = None,
    ) -> PaidMediaOperationOutcome:
        if isinstance(outcome, PaidMediaDelivered):
            category = "delivered"
        elif isinstance(outcome, PaidMediaAwaitingFunding):
            category = f"awaiting_funding:{outcome.reason.value}"
        elif isinstance(outcome, PaidMediaNotCharged):
            category = f"not_charged:{outcome.reason.value}"
        elif isinstance(outcome, PaidMediaRefunded):
            category = "refunded"
        elif isinstance(outcome, PaidMediaDeliveryUncertain):
            category = "delivery_uncertain"
        elif isinstance(outcome, PaidMediaInProgress):
            category = f"in_progress:{outcome.stage.value}"
        else:  # pragma: no cover - closed outcome union
            raise TypeError(f"unsupported paid media outcome: {type(outcome).__name__}")
        record_operation_outcome(
            span,
            category,
            authorization=authorization,
        )
        return outcome

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("paid media operation clock must be timezone-aware")
        return now


__all__ = [
    "PaidMediaAwaitingFunding",
    "PaidMediaDelivered",
    "PaidMediaDeliveryUncertain",
    "PaidMediaExecutor",
    "PaidMediaInProgress",
    "PaidMediaInvocation",
    "PaidMediaNotCharged",
    "PaidMediaNotChargedReason",
    "PaidMediaOperationCoordinator",
    "PaidMediaOperationOutcome",
    "PaidMediaQuoteInput",
    "PaidMediaRefunded",
    "PaidMediaResult",
]
