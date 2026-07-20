"""Resumable settlement and delivery for paid image operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import logfire

from derp.artifacts import ArtifactStoreError
from derp.delivery.types import (
    Delivered,
    DeliveryTarget,
    DeliveryUncertain,
    ProgressStage,
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
from derp.features.image import (
    ImageEditRequest,
    ImageFeatureService,
    ImageGenerateRequest,
    ImageOutput,
)
from derp.operations import (
    CompositeImageQuoteInput,
    DeliveryState,
    FinishingChatQuoteInput,
    ImageEditQuoteInput,
    ImageGenerateQuoteInput,
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
)

if TYPE_CHECKING:
    from derp.delivery.service import DeliveryService

type ImageRequest = ImageGenerateRequest | ImageEditRequest


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_operation_id(value: OperationId) -> None:
    if not isinstance(value, OperationId):
        raise TypeError("operation_id must be an OperationId")


@dataclass(frozen=True, slots=True)
class ImageInvocation:
    """Stable ownership, scope, pricing, and delivery inputs for one image run."""

    operation_id: OperationId
    request_key: str
    requester_id: uuid.UUID
    chat_id: uuid.UUID
    thread_id: int | None
    target: DeliveryTarget
    input_tokens: int
    caption: str | None = field(default=None, repr=False)

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
        if isinstance(self.input_tokens, bool) or not isinstance(
            self.input_tokens, int
        ):
            raise TypeError("input_tokens must be an integer")
        if self.input_tokens < 0:
            raise ValueError("input_tokens must not be negative")
        if self.caption is not None:
            if not isinstance(self.caption, str):
                raise TypeError("caption must be a string")
            caption = self.caption.strip()
            if not caption:
                raise ValueError("caption must not be blank")
            object.__setattr__(self, "caption", caption)


class ImageNotChargedReason(StrEnum):
    """Stable terminal reasons that adapters may translate into user copy."""

    QUOTE_EXPIRED = "quote_expired"
    INVALID_INPUT = "invalid_input"
    POLICY_REJECTION = "policy_rejection"
    UNUSABLE_OUTPUT = "unusable_output"
    PROVIDER_FAILURE = "provider_failure"
    RESULT_STORAGE_FAILURE = "result_storage_failure"
    RELEASED = "released"
    CANCELED = "canceled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ImageAwaitingFunding:
    """The exact quote remains unreserved and needs one funding decision."""

    operation_id: OperationId
    quote: Quote
    reason: ReservationRejection

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.quote, Quote):
            raise TypeError("quote must be a Quote")
        if self.quote.operation_id != self.operation_id:
            raise ValueError("quote must belong to the operation")
        if not isinstance(self.reason, ReservationRejection):
            raise TypeError("reason must be a ReservationRejection")
        if self.reason not in {
            ReservationRejection.PERSONAL_CONSENT_REQUIRED,
            ReservationRejection.INSUFFICIENT_FUNDS,
            ReservationRejection.WALLET_IN_DEBT,
        }:
            raise ValueError("awaiting funding requires an actionable funding reason")


@dataclass(frozen=True, slots=True)
class ImageInProgress:
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
            if not self.code.strip():
                raise ValueError("in-progress code must not be blank")


@dataclass(frozen=True, slots=True)
class ImageDelivered:
    """Telegram acknowledged the operation's persisted image result."""

    operation_id: OperationId
    message_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.message_ids, tuple):
            raise TypeError("delivered message IDs must use an immutable tuple")
        if not self.message_ids or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.message_ids
        ):
            raise ValueError("delivered message IDs must be positive")


@dataclass(frozen=True, slots=True)
class ImageDeliveryUncertain:
    """Telegram may have accepted the result; automatic resend is forbidden."""

    operation_id: OperationId
    code: str
    resend_token: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.code, str):
            raise TypeError("delivery uncertainty code must be a string")
        if not self.code.strip():
            raise ValueError("delivery uncertainty code must not be blank")
        if not isinstance(self.resend_token, str) or not self.resend_token.strip():
            raise ValueError("resend token must not be blank")


@dataclass(frozen=True, slots=True)
class ImageNotCharged:
    """The operation ended before capture and its reservation was released."""

    operation_id: OperationId
    reason: ImageNotChargedReason

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.reason, ImageNotChargedReason):
            raise TypeError("reason must be an ImageNotChargedReason")


@dataclass(frozen=True, slots=True)
class ImageRefunded:
    """Captured spend was returned after terminal delivery failure."""

    operation_id: OperationId
    code: str

    def __post_init__(self) -> None:
        _require_operation_id(self.operation_id)
        if not isinstance(self.code, str):
            raise TypeError("refund code must be a string")
        if not self.code.strip():
            raise ValueError("refund code must not be blank")


type ImageOperationOutcome = (
    ImageAwaitingFunding
    | ImageInProgress
    | ImageDelivered
    | ImageDeliveryUncertain
    | ImageNotCharged
    | ImageRefunded
)


class ImageOperationCoordinator:
    """Run or safely resume one quote-to-delivery paid image operation."""

    def __init__(
        self,
        ledger: OperationLedger,
        quote_engine: QuoteEngine,
        image_service: ImageFeatureService,
        delivery_service: DeliveryService,
        *,
        clock: Callable[[], datetime] = _utc_now,
        quote_id_factory: Callable[[], QuoteId] = QuoteId.new,
    ) -> None:
        self._ledger = ledger
        self._quote_engine = quote_engine
        self._image_service = image_service
        self._delivery_service = delivery_service
        self._clock = clock
        self._quote_id_factory = quote_id_factory

    async def run(
        self,
        invocation: ImageInvocation,
        plan: ExecutionPlan,
        request: ImageRequest,
        *,
        allow_personal_once: bool = False,
        finishing_plan: ExecutionPlan | None = None,
        finishing_quote_input: FinishingChatQuoteInput | None = None,
    ) -> ImageOperationOutcome:
        """Execute only a newly claimed provider call; otherwise resume safely."""
        feature = self._require_matching_feature(plan, request)

        with logfire.span(
            "image.operation",
            operation_id=str(invocation.operation_id),
            feature=feature.value,
        ):
            quote = await self.ensure_quote(
                invocation,
                plan,
                request,
                finishing_plan=finishing_plan,
                finishing_quote_input=finishing_quote_input,
            )
            reservation = await self._ledger.reserve(
                invocation.operation_id,
                allow_personal_once=allow_personal_once,
            )
            if isinstance(reservation, ReservationRejected):
                return await self._reservation_rejected(reservation, quote)

            if reservation.idempotent:
                snapshot = await self._ledger.get_snapshot(invocation.operation_id)
                if not snapshot.can_claim_execution:
                    return await self._resume(snapshot)

            try:
                claim = await self._ledger.mark_executing(invocation.operation_id)
            except InvalidOperationTransitionError:
                return await self._resume_operation(invocation.operation_id)
            if not claim.execution_claimed:
                return await self._resume_operation(invocation.operation_id)

            provider_outcome = await self._execute(plan, request)
            if isinstance(provider_outcome, Rejected):
                reason = self._rejection_reason(provider_outcome.reason)
                await self._ledger.release(
                    invocation.operation_id,
                    reason=f"image_{provider_outcome.reason.value}",
                )
                return ImageNotCharged(invocation.operation_id, reason)
            if isinstance(provider_outcome, Failed):
                await self._ledger.release(
                    invocation.operation_id,
                    reason=f"image_{provider_outcome.reason.value}",
                )
                return ImageNotCharged(
                    invocation.operation_id,
                    ImageNotChargedReason.PROVIDER_FAILURE,
                )
            if not isinstance(provider_outcome, Succeeded):
                raise RuntimeError("image service returned an unsupported outcome")

            try:
                prepared = await self._delivery_service.persist_result(
                    invocation.operation_id,
                    media=provider_outcome.value.images,
                    target=invocation.target,
                    caption=invocation.caption,
                )
            except ArtifactStoreError:
                await self._ledger.release(
                    invocation.operation_id,
                    reason="image_result_storage_failed",
                )
                return ImageNotCharged(
                    invocation.operation_id,
                    ImageNotChargedReason.RESULT_STORAGE_FAILURE,
                )

            await self._ledger.capture(invocation.operation_id)
            await self._delivery_service.mark_ready(invocation.operation_id)
            return await self._deliver(
                invocation.operation_id,
                resend_token=prepared.resend_token,
            )

    async def ensure_quote(
        self,
        invocation: ImageInvocation,
        plan: ExecutionPlan,
        request: ImageRequest,
        *,
        finishing_plan: ExecutionPlan | None = None,
        finishing_quote_input: FinishingChatQuoteInput | None = None,
    ) -> Quote:
        """Persist or recover the exact immutable quote without reserving funds."""
        self._require_matching_feature(plan, request)
        if (finishing_plan is None) != (finishing_quote_input is None):
            raise ValueError("finishing plan and quote input must be provided together")
        image_quote_input = self._quote_input(invocation, request)
        if finishing_plan is None or finishing_quote_input is None:
            proposed_quote = self._quote_engine.quote(
                quote_id=self._quote_id_factory(),
                operation_id=invocation.operation_id,
                plan=plan,
                quote_input=image_quote_input,
                created_at=self._aware_now(),
            )
        else:
            proposed_quote = self._quote_engine.quote_composite_image(
                quote_id=self._quote_id_factory(),
                operation_id=invocation.operation_id,
                image_plan=plan,
                finishing_plan=finishing_plan,
                quote_input=CompositeImageQuoteInput(
                    image=image_quote_input,
                    finishing=finishing_quote_input,
                ),
                created_at=self._aware_now(),
            )
        return await self._ledger.ensure_quote(
            proposed_quote,
            request_key=invocation.request_key,
            requester_id=invocation.requester_id,
            chat_id=invocation.chat_id,
            thread_id=invocation.thread_id,
            pricing_input=self._pricing_input(
                invocation,
                request,
                finishing_quote_input=finishing_quote_input,
            ),
        )

    async def _reservation_rejected(
        self,
        rejection: ReservationRejected,
        quote: Quote,
    ) -> ImageOperationOutcome:
        if rejection.reason in {
            ReservationRejection.PERSONAL_CONSENT_REQUIRED,
            ReservationRejection.INSUFFICIENT_FUNDS,
            ReservationRejection.WALLET_IN_DEBT,
        }:
            return ImageAwaitingFunding(rejection.operation_id, quote, rejection.reason)
        if rejection.reason is ReservationRejection.QUOTE_EXPIRED:
            return ImageNotCharged(
                rejection.operation_id,
                ImageNotChargedReason.QUOTE_EXPIRED,
            )
        return await self._resume_operation(rejection.operation_id)

    async def _resume_operation(
        self,
        operation_id: OperationId,
    ) -> ImageOperationOutcome:
        return await self._resume(await self._ledger.get_snapshot(operation_id))

    async def _resume(
        self,
        snapshot: OperationSnapshot,
    ) -> ImageOperationOutcome:
        operation_id = snapshot.operation_id
        if snapshot.state is OperationState.EXECUTING:
            artifact_count = snapshot.result_metadata.get("artifact_count")
            if (
                isinstance(artifact_count, int)
                and not isinstance(artifact_count, bool)
                and artifact_count > 0
            ):
                await self._ledger.capture(operation_id)
                await self._delivery_service.mark_ready(operation_id)
                return await self._deliver(operation_id)
            return ImageInProgress(operation_id, ProgressStage.GENERATING)
        if snapshot.state is OperationState.CAPTURED:
            if snapshot.delivery_state is DeliveryState.NOT_READY:
                await self._delivery_service.mark_ready(operation_id)
            return await self._deliver(operation_id)
        if snapshot.state is OperationState.REVERSED:
            return ImageRefunded(
                operation_id,
                snapshot.terminal_reason or "delivery_failed",
            )
        if snapshot.state is OperationState.RELEASED:
            return ImageNotCharged(operation_id, ImageNotChargedReason.RELEASED)
        if snapshot.state is OperationState.CANCELED:
            return ImageNotCharged(operation_id, ImageNotChargedReason.CANCELED)
        if snapshot.state is OperationState.FAILED:
            return ImageNotCharged(operation_id, ImageNotChargedReason.FAILED)
        return ImageInProgress(operation_id, ProgressStage.PREPARING)

    async def _deliver(
        self,
        operation_id: OperationId,
        *,
        resend_token: str | None = None,
    ) -> ImageOperationOutcome:
        outcome = await self._delivery_service.deliver(operation_id)
        if isinstance(outcome, Delivered):
            return ImageDelivered(operation_id, outcome.message_ids)
        if isinstance(outcome, DeliveryUncertain):
            if outcome.code == "attempt_in_progress_or_interrupted":
                return ImageInProgress(
                    operation_id,
                    ProgressStage.DELIVERING,
                    outcome.code,
                )
            if resend_token is None:
                resend_token = await self._delivery_service.issue_resend_token(
                    operation_id
                )
            return ImageDeliveryUncertain(operation_id, outcome.code, resend_token)
        if outcome.retryable:
            return ImageInProgress(
                operation_id,
                ProgressStage.DELIVERING,
                outcome.code,
            )
        snapshot = await self._ledger.get_snapshot(operation_id)
        if snapshot.state is not OperationState.REVERSED:
            raise RuntimeError(
                "terminal delivery failure did not reverse captured spend"
            )
        return ImageRefunded(operation_id, outcome.code)

    async def _execute(
        self,
        plan: ExecutionPlan,
        request: ImageRequest,
    ) -> Outcome[ImageOutput]:
        if isinstance(request, ImageGenerateRequest):
            return await self._image_service.generate(plan, request)
        return await self._image_service.edit(plan, request)

    @staticmethod
    def _feature_for(request: ImageRequest) -> Feature:
        if isinstance(request, ImageGenerateRequest):
            return Feature.IMAGE_GENERATE
        if isinstance(request, ImageEditRequest):
            return Feature.IMAGE_EDIT
        raise TypeError("request must be an image generate or edit request")

    @classmethod
    def _require_matching_feature(
        cls,
        plan: ExecutionPlan,
        request: ImageRequest,
    ) -> Feature:
        feature = cls._feature_for(request)
        if plan.feature is not feature:
            raise ValueError(f"{plan.feature.value} cannot execute {feature.value}")
        return feature

    @staticmethod
    def _quote_input(
        invocation: ImageInvocation,
        request: ImageRequest,
    ) -> ImageGenerateQuoteInput | ImageEditQuoteInput:
        if isinstance(request, ImageGenerateRequest):
            return ImageGenerateQuoteInput(
                invocation.input_tokens,
                request.resolution,
            )
        return ImageEditQuoteInput(invocation.input_tokens, request.resolution)

    @classmethod
    def _pricing_input(
        cls,
        invocation: ImageInvocation,
        request: ImageRequest,
        *,
        finishing_quote_input: FinishingChatQuoteInput | None = None,
    ) -> Mapping[str, object]:
        pricing_input: dict[str, object] = {
            "input_tokens": invocation.input_tokens,
            "resolution": request.resolution.value,
            "request_fingerprint": cls._request_fingerprint(request),
            "delivery_fingerprint": cls._delivery_fingerprint(invocation),
        }
        if finishing_quote_input is not None:
            pricing_input.update(
                {
                    "finishing_model_key": finishing_quote_input.model_key.value,
                    "finishing_input_tokens": finishing_quote_input.input_tokens,
                }
            )
        return pricing_input

    @staticmethod
    def _request_fingerprint(request: ImageRequest) -> str:
        if isinstance(request, ImageGenerateRequest):
            value = {
                "feature": Feature.IMAGE_GENERATE.value,
                "prompt": request.prompt,
                "style": request.style,
                "resolution": request.resolution.value,
            }
        else:
            value = {
                "feature": Feature.IMAGE_EDIT.value,
                "prompt": request.prompt,
                "source_unique_id": request.source.file_unique_id,
                "source_mime_type": request.source.metadata.mime_type,
                "resolution": request.resolution.value,
            }
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()

    @staticmethod
    def _delivery_fingerprint(invocation: ImageInvocation) -> str:
        value = {
            "chat_id": invocation.target.chat_id,
            "thread_id": invocation.target.thread_id,
            "reply_to_message_id": invocation.target.reply_to_message_id,
            "business_connection_id": invocation.target.business_connection_id,
            "caption": invocation.caption,
        }
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()

    @staticmethod
    def _rejection_reason(reason: RejectionReason) -> ImageNotChargedReason:
        return {
            RejectionReason.INVALID_INPUT: ImageNotChargedReason.INVALID_INPUT,
            RejectionReason.POLICY: ImageNotChargedReason.POLICY_REJECTION,
            RejectionReason.UNUSABLE_OUTPUT: ImageNotChargedReason.UNUSABLE_OUTPUT,
        }[reason]

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("image operation clock must be timezone-aware")
        return now


__all__ = [
    "ImageAwaitingFunding",
    "ImageDelivered",
    "ImageDeliveryUncertain",
    "ImageInProgress",
    "ImageInvocation",
    "ImageNotCharged",
    "ImageNotChargedReason",
    "ImageOperationCoordinator",
    "ImageOperationOutcome",
    "ImageRefunded",
    "ImageRequest",
]
