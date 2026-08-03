"""Governed unlimited free inline chat with per-request inference accounting."""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

import logfire

from derp.catalog import InferenceProvider, ModelRole
from derp.execution import (
    ExecutionPlan,
    Failed,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.types import TextOutput
from derp.inference.privacy import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    InferenceContext,
    InferencePrivacyPreference,
    decide_non_zdr_free_inference,
)
from derp.inference.recorder import InferenceAttempt, InferenceRecorder
from derp.inference_types import InferenceReport
from derp.observability import report_exception

MAX_INLINE_QUERY_CHARS: Final = 256
MAX_INLINE_QUERY_BYTES: Final = 1_024
MAX_INLINE_OUTPUT_CHARS: Final = 2_000
MAX_INLINE_OUTPUT_BYTES: Final = 8_000
MAX_INLINE_OUTPUT_TOKENS: Final = 256
INLINE_CHAT_PLAN: Final = plan_execution(
    Feature.INLINE_CHAT,
    ModelRole.CHAT_ECONOMY,
)
FREE_INLINE_CHAT_PLAN: Final = plan_execution(
    Feature.INLINE_CHAT,
    ModelRole.FREE_TEXT,
    provider=InferenceProvider.OPENROUTER,
)


@dataclass(frozen=True, slots=True)
class InlineChatRequest:
    """Normalized inline query that is safe to submit as the sole user prompt."""

    query: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise TypeError("inline query must be a string")
        query = self.query.strip()
        if not query:
            raise ValueError("inline query must not be blank")
        if "\x00" in query:
            raise ValueError("inline query must not contain NUL characters")
        if len(query) > MAX_INLINE_QUERY_CHARS:
            raise ValueError(
                f"inline query must be at most {MAX_INLINE_QUERY_CHARS} characters"
            )
        if len(query.encode("utf-8")) > MAX_INLINE_QUERY_BYTES:
            raise ValueError(
                f"inline query must be at most {MAX_INLINE_QUERY_BYTES} UTF-8 bytes"
            )
        object.__setattr__(self, "query", query)


@dataclass(frozen=True, slots=True)
class PreparedInlineChatRequest:
    """Provider input plus explicit Pydantic AI usage ceilings."""

    query: str = field(repr=False)
    max_output_tokens: int
    input_tokens_limit: int

    def __post_init__(self) -> None:
        request = InlineChatRequest(self.query)
        for name in ("max_output_tokens", "input_tokens_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_output_tokens > MAX_INLINE_OUTPUT_TOKENS:
            raise ValueError(
                f"max_output_tokens must not exceed {MAX_INLINE_OUTPUT_TOKENS}"
            )
        object.__setattr__(self, "query", request.query)


@dataclass(frozen=True, slots=True)
class InlineChatInvocation:
    """Content-hidden identity and privacy preference for one chosen result."""

    request_id: uuid.UUID
    user_id: uuid.UUID
    query: str = field(repr=False)
    privacy: InferencePrivacyPreference

    def __post_init__(self) -> None:
        for name in ("request_id", "user_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if not isinstance(self.query, str):
            raise TypeError("query must be a string")
        if not isinstance(self.privacy, InferencePrivacyPreference):
            raise TypeError("privacy must be an InferencePrivacyPreference")


class InlineChatProviderExecutor(Protocol):
    """Provider adapter with no persistence or Telegram effects."""

    async def answer(
        self,
        plan: ExecutionPlan,
        request: PreparedInlineChatRequest,
        *,
        user_id: uuid.UUID,
    ) -> InlineProviderExecution:
        """Return one provider-independent text result."""
        ...


@dataclass(frozen=True, slots=True)
class InlineProviderExecution:
    """One provider result plus content-free accounting reports."""

    outcome: Outcome[TextOutput] = field(repr=False)
    reports: tuple[InferenceReport, ...] = ()
    provider_completed: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.outcome, Succeeded) and not isinstance(
            self.outcome.value, TextOutput
        ):
            raise TypeError("successful inline execution must contain TextOutput")
        if not isinstance(self.outcome, Succeeded | Rejected | Failed):
            raise TypeError("outcome must be a supported provider outcome")
        if any(not isinstance(report, InferenceReport) for report in self.reports):
            raise TypeError("reports must contain only InferenceReport values")
        if not isinstance(self.provider_completed, bool):
            raise TypeError("provider_completed must be a bool")
        if not self.provider_completed and self.reports:
            raise ValueError("an incomplete provider call cannot contain reports")


class InlineChatFailureReason(StrEnum):
    """Stable failure categories exposed to the Telegram adapter."""

    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"
    FREE_MODE_REQUIRED = "free_mode_required"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_REJECTED = "provider_rejected"
    UNUSABLE_OUTPUT = "unusable_output"


@dataclass(frozen=True, slots=True)
class InlineChatCompleted:
    """One bounded answer was produced."""

    text: str = field(repr=False)

    def __post_init__(self) -> None:
        output = TextOutput(self.text)
        object.__setattr__(self, "text", output.text)


@dataclass(frozen=True, slots=True)
class InlineChatInvalid:
    """The query was rejected before provider execution."""


@dataclass(frozen=True, slots=True)
class InlineChatFailed:
    """Accounting or provider execution failed without private details."""

    reason: InlineChatFailureReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, InlineChatFailureReason):
            raise TypeError("reason must be an InlineChatFailureReason")


type InlineChatOutcome = InlineChatCompleted | InlineChatInvalid | InlineChatFailed


@dataclass(frozen=True, slots=True)
class InlineChatPolicy:
    """Hard per-request transport and output limits for free inline answers."""

    provider_deadline_seconds: float = 20.0
    max_output_tokens: int = MAX_INLINE_OUTPUT_TOKENS
    max_output_chars: int = MAX_INLINE_OUTPUT_CHARS
    max_output_bytes: int = MAX_INLINE_OUTPUT_BYTES
    input_tokens_limit: int = 1_024

    def __post_init__(self) -> None:
        for name in (
            "max_output_tokens",
            "max_output_chars",
            "max_output_bytes",
            "input_tokens_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_output_tokens > MAX_INLINE_OUTPUT_TOKENS:
            raise ValueError(
                f"max_output_tokens must not exceed {MAX_INLINE_OUTPUT_TOKENS}"
            )
        if self.max_output_chars > MAX_INLINE_OUTPUT_CHARS:
            raise ValueError(
                f"max_output_chars must not exceed {MAX_INLINE_OUTPUT_CHARS}"
            )
        if self.max_output_bytes > MAX_INLINE_OUTPUT_BYTES:
            raise ValueError(
                f"max_output_bytes must not exceed {MAX_INLINE_OUTPUT_BYTES}"
            )
        if (
            isinstance(self.provider_deadline_seconds, bool)
            or not isinstance(self.provider_deadline_seconds, (int, float))
            or not math.isfinite(self.provider_deadline_seconds)
            or self.provider_deadline_seconds <= 0
        ):
            raise ValueError("provider_deadline_seconds must be finite and positive")


class InlineChatFeatureService:
    """Govern consent, accounting, and bounded zero-cost inline execution."""

    def __init__(
        self,
        executor: InlineChatProviderExecutor,
        inference_recorder: InferenceRecorder,
        *,
        free_plan: ExecutionPlan | None,
        policy: InlineChatPolicy | None = None,
    ) -> None:
        self._executor = executor
        if free_plan is not None and (
            free_plan.feature is not Feature.INLINE_CHAT
            or free_plan.model.key is not ModelRole.FREE_TEXT
            or free_plan.model.provider is not InferenceProvider.OPENROUTER
            or not free_plan.model.available
        ):
            raise ValueError("free inline chat requires the OpenRouter free-text plan")
        self._inference_recorder = inference_recorder
        self._free_plan = free_plan
        self._policy = policy or InlineChatPolicy()

    async def answer(self, invocation: InlineChatInvocation) -> InlineChatOutcome:
        """Return one typed inline outcome without retaining query content."""
        if not isinstance(invocation, InlineChatInvocation):
            raise TypeError("invocation must be an InlineChatInvocation")
        plan = self._select_plan(invocation.privacy)
        if plan is None:
            return InlineChatFailed(InlineChatFailureReason.FREE_MODE_REQUIRED)
        with logfire.span(
            "inline_chat.operation",
            feature=Feature.INLINE_CHAT.value,
            model_key=plan.model.key.value,
            provider=plan.model.provider.value,
            privacy_revision=invocation.privacy.revision,
            free_model=True,
        ) as span:
            try:
                request = InlineChatRequest(invocation.query)
            except TypeError, ValueError:
                return self._record_outcome(span, InlineChatInvalid())

            span.set_attribute("derp.inline.query_chars", len(request.query))
            span.set_attribute(
                "derp.inline.query_bytes", len(request.query.encode("utf-8"))
            )
            prepared = PreparedInlineChatRequest(
                request.query,
                max_output_tokens=self._policy.max_output_tokens,
                input_tokens_limit=self._policy.input_tokens_limit,
            )
            provider_outcome = await self._run_provider(
                plan,
                prepared,
                user_id=invocation.user_id,
            )
            if not isinstance(provider_outcome, Succeeded):
                outcome = InlineChatFailed(
                    provider_outcome,
                )
                return self._record_outcome(span, outcome)

            output = provider_outcome.value
            if (
                len(output.text) > self._policy.max_output_chars
                or len(output.text.encode("utf-8")) > self._policy.max_output_bytes
            ):
                outcome = InlineChatFailed(
                    InlineChatFailureReason.UNUSABLE_OUTPUT,
                )
                return self._record_outcome(span, outcome)

            span.set_attribute("derp.inline.output_chars", len(output.text))
            outcome = InlineChatCompleted(output.text)
            return self._record_outcome(span, outcome)

    def _select_plan(
        self,
        preference: InferencePrivacyPreference,
    ) -> ExecutionPlan | None:
        decision = decide_non_zdr_free_inference(
            preference,
            context=InferenceContext.INLINE,
            current_tos_version=FREE_INFERENCE_TOS_VERSION,
            current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        )
        if decision.allowed and self._free_plan is not None:
            return self._free_plan
        return None

    async def _run_provider(
        self,
        plan: ExecutionPlan,
        request: PreparedInlineChatRequest,
        *,
        user_id: uuid.UUID,
    ) -> Succeeded[TextOutput] | InlineChatFailureReason:
        try:
            attempt = await self._inference_recorder.start(
                model=plan.model,
                user_id=user_id,
                chat_id=None,
                operation_id=None,
            )
        except Exception as exc:
            report_exception(
                "inline_inference_start_failed",
                exception=exc,
                level="warning",
                model_key=plan.model.key.value,
            )
            return InlineChatFailureReason.ACCOUNTING_UNAVAILABLE
        try:
            async with asyncio.timeout(self._policy.provider_deadline_seconds):
                execution = await self._executor.answer(
                    plan,
                    request,
                    user_id=user_id,
                )
                if not isinstance(execution, InlineProviderExecution):
                    raise TypeError(
                        "inline executor must return InlineProviderExecution"
                    )
        except TimeoutError:
            await self._record_failed_inference(attempt)
            return InlineChatFailureReason.PROVIDER_TIMEOUT
        except Exception as exc:
            await self._record_failed_inference(attempt)
            report_exception(
                "inline_chat_provider_failed",
                exception=exc,
                level="warning",
                model_key=plan.model.key.value,
            )
            return InlineChatFailureReason.PROVIDER_ERROR
        try:
            if execution.provider_completed:
                await self._inference_recorder.succeed_reports(
                    attempt,
                    execution.reports,
                )
            else:
                await self._inference_recorder.fail(attempt)
        except Exception as exc:
            report_exception(
                "inline_inference_completion_failed",
                exception=exc,
                level="warning",
                inference_usage_id=str(attempt.id),
            )
            return InlineChatFailureReason.ACCOUNTING_UNAVAILABLE
        outcome = execution.outcome
        if isinstance(outcome, Succeeded) and isinstance(outcome.value, TextOutput):
            return outcome
        if isinstance(outcome, Rejected):
            return (
                InlineChatFailureReason.UNUSABLE_OUTPUT
                if outcome.reason is RejectionReason.UNUSABLE_OUTPUT
                else InlineChatFailureReason.PROVIDER_REJECTED
            )
        if isinstance(outcome, Failed):
            return InlineChatFailureReason.PROVIDER_ERROR
        return InlineChatFailureReason.PROVIDER_ERROR

    async def _record_failed_inference(self, attempt: InferenceAttempt) -> None:
        try:
            await self._inference_recorder.fail(attempt)
        except Exception as exc:
            report_exception(
                "inline_inference_failure_recording_failed",
                exception=exc,
                level="warning",
                inference_usage_id=str(attempt.id),
            )

    @staticmethod
    def _record_outcome(
        span: logfire.LogfireSpan,
        outcome: InlineChatOutcome,
    ) -> InlineChatOutcome:
        if isinstance(outcome, InlineChatCompleted):
            category = "completed"
        elif isinstance(outcome, InlineChatInvalid):
            category = "invalid"
        elif isinstance(outcome, InlineChatFailed):
            category = f"failed:{outcome.reason.value}"
        else:  # pragma: no cover - closed outcome union
            raise TypeError(f"unsupported inline outcome: {type(outcome).__name__}")
        span.set_attribute("derp.inline.outcome", category)
        return outcome


__all__ = [
    "FREE_INLINE_CHAT_PLAN",
    "INLINE_CHAT_PLAN",
    "MAX_INLINE_OUTPUT_BYTES",
    "MAX_INLINE_OUTPUT_CHARS",
    "MAX_INLINE_OUTPUT_TOKENS",
    "MAX_INLINE_QUERY_BYTES",
    "MAX_INLINE_QUERY_CHARS",
    "InlineChatCompleted",
    "InlineChatFailed",
    "InlineChatFailureReason",
    "InlineChatFeatureService",
    "InlineChatInvalid",
    "InlineChatInvocation",
    "InlineChatOutcome",
    "InlineChatPolicy",
    "InlineChatProviderExecutor",
    "InlineChatRequest",
    "InlineProviderExecution",
    "PreparedInlineChatRequest",
]
