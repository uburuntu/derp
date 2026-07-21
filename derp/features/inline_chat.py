"""Bounded free inline chat with atomic daily admission."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Final, Protocol

import logfire

from derp.catalog import GoogleModelKey
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
from derp.observability import report_exception

MAX_INLINE_QUERY_CHARS: Final = 256
MAX_INLINE_QUERY_BYTES: Final = 1_024
MAX_INLINE_OUTPUT_CHARS: Final = 2_000
MAX_INLINE_OUTPUT_BYTES: Final = 8_000
MAX_INLINE_OUTPUT_TOKENS: Final = 256
MAX_INLINE_DAILY_REQUESTS: Final = 100
DEFAULT_INLINE_DAILY_REQUESTS: Final = 10
INLINE_CHAT_PLAN: Final = plan_execution(
    Feature.INLINE_CHAT,
    GoogleModelKey.CHAT_ECONOMY,
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
class InlineAllowanceGranted:
    """One provider attempt was atomically admitted for the UTC day."""

    usage_date: date
    used_count: int
    limit: int

    def __post_init__(self) -> None:
        _validate_allowance_count(self.used_count, self.limit)
        if self.used_count > self.limit:
            raise ValueError("granted allowance cannot exceed its limit")

    @property
    def remaining(self) -> int:
        return self.limit - self.used_count


@dataclass(frozen=True, slots=True)
class InlineAllowanceExhausted:
    """No provider attempt was admitted for the UTC day."""

    usage_date: date
    used_count: int
    limit: int

    def __post_init__(self) -> None:
        _validate_allowance_count(self.used_count, self.limit)
        if self.used_count < self.limit:
            raise ValueError("exhausted allowance must have reached its limit")


type InlineAllowanceClaim = InlineAllowanceGranted | InlineAllowanceExhausted


class InlineAllowanceClaimer(Protocol):
    """Atomic admission boundary implemented by PostgreSQL."""

    async def claim(
        self,
        *,
        user_id: uuid.UUID,
        usage_date: date,
        limit: int,
    ) -> InlineAllowanceClaim:
        """Admit at most ``limit`` provider attempts for one user and day."""
        ...


class InlineChatProviderExecutor(Protocol):
    """Provider adapter with no persistence or Telegram effects."""

    async def answer(
        self,
        plan: ExecutionPlan,
        request: PreparedInlineChatRequest,
    ) -> Outcome[TextOutput]:
        """Return one provider-independent text result."""
        ...


class InlineChatFailureReason(StrEnum):
    """Stable failure categories exposed to the Telegram adapter."""

    ALLOWANCE_UNAVAILABLE = "allowance_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_REJECTED = "provider_rejected"
    UNUSABLE_OUTPUT = "unusable_output"


@dataclass(frozen=True, slots=True)
class InlineChatCompleted:
    """One bounded answer was produced after a successful allowance claim."""

    text: str = field(repr=False)
    allowance_remaining: int

    def __post_init__(self) -> None:
        output = TextOutput(self.text)
        _validate_nonnegative_count(
            "allowance_remaining",
            self.allowance_remaining,
        )
        object.__setattr__(self, "text", output.text)


@dataclass(frozen=True, slots=True)
class InlineChatInvalid:
    """The query was rejected before any allowance was consumed."""


@dataclass(frozen=True, slots=True)
class InlineChatExhausted:
    """The user's UTC-day provider allowance is exhausted."""

    reset_at: datetime
    limit: int

    def __post_init__(self) -> None:
        if self.reset_at.tzinfo is None or self.reset_at.utcoffset() is None:
            raise ValueError("reset_at must be timezone-aware")
        _validate_positive_count("limit", self.limit)


@dataclass(frozen=True, slots=True)
class InlineChatFailed:
    """Admission or provider execution failed without exposing private details."""

    reason: InlineChatFailureReason
    allowance_remaining: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, InlineChatFailureReason):
            raise TypeError("reason must be an InlineChatFailureReason")
        if self.allowance_remaining is not None:
            _validate_nonnegative_count(
                "allowance_remaining",
                self.allowance_remaining,
            )


type InlineChatOutcome = (
    InlineChatCompleted | InlineChatInvalid | InlineChatExhausted | InlineChatFailed
)


@dataclass(frozen=True, slots=True)
class InlineChatPolicy:
    """Hard cost, transport, and output limits for free inline answers."""

    daily_requests: int = DEFAULT_INLINE_DAILY_REQUESTS
    provider_deadline_seconds: float = 20.0
    max_output_tokens: int = MAX_INLINE_OUTPUT_TOKENS
    max_output_chars: int = MAX_INLINE_OUTPUT_CHARS
    max_output_bytes: int = MAX_INLINE_OUTPUT_BYTES
    input_tokens_limit: int = 1_024

    def __post_init__(self) -> None:
        for name in (
            "daily_requests",
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
        if self.daily_requests > MAX_INLINE_DAILY_REQUESTS:
            raise ValueError(
                f"daily_requests must not exceed {MAX_INLINE_DAILY_REQUESTS}"
            )
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
    """Claim one bounded free attempt, then execute outside the transaction."""

    def __init__(
        self,
        allowance: InlineAllowanceClaimer,
        executor: InlineChatProviderExecutor,
        *,
        policy: InlineChatPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._allowance = allowance
        self._executor = executor
        self._policy = policy or InlineChatPolicy()
        self._clock = clock or _utc_now

    async def answer(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
    ) -> InlineChatOutcome:
        """Return one typed inline outcome without retaining query content."""
        if not isinstance(user_id, uuid.UUID):
            raise TypeError("user_id must be a UUID")
        with logfire.span(
            "inline_chat.operation",
            feature=Feature.INLINE_CHAT.value,
            model_key=INLINE_CHAT_PLAN.model.key.value,
        ) as span:
            try:
                request = InlineChatRequest(query)
            except TypeError, ValueError:
                return self._record_outcome(span, InlineChatInvalid())

            span.set_attribute("derp.inline.query_chars", len(request.query))
            span.set_attribute(
                "derp.inline.query_bytes", len(request.query.encode("utf-8"))
            )
            usage_date = self._aware_now().astimezone(UTC).date()
            try:
                claim = await self._allowance.claim(
                    user_id=user_id,
                    usage_date=usage_date,
                    limit=self._policy.daily_requests,
                )
            except Exception as exc:
                report_exception(
                    "inline_allowance_claim_failed",
                    exception=exc,
                    level="warning",
                )
                outcome: InlineChatOutcome = InlineChatFailed(
                    InlineChatFailureReason.ALLOWANCE_UNAVAILABLE,
                    None,
                )
                return self._record_outcome(span, outcome)

            if isinstance(claim, InlineAllowanceExhausted):
                outcome = InlineChatExhausted(
                    reset_at=datetime.combine(
                        usage_date + timedelta(days=1),
                        time.min,
                        tzinfo=UTC,
                    ),
                    limit=claim.limit,
                )
                return self._record_outcome(span, outcome)

            prepared = PreparedInlineChatRequest(
                request.query,
                max_output_tokens=self._policy.max_output_tokens,
                input_tokens_limit=self._policy.input_tokens_limit,
            )
            provider_outcome = await self._run_provider(prepared)
            if not isinstance(provider_outcome, Succeeded):
                outcome = InlineChatFailed(
                    provider_outcome,
                    claim.remaining,
                )
                return self._record_outcome(span, outcome)

            output = provider_outcome.value
            if (
                len(output.text) > self._policy.max_output_chars
                or len(output.text.encode("utf-8")) > self._policy.max_output_bytes
            ):
                outcome = InlineChatFailed(
                    InlineChatFailureReason.UNUSABLE_OUTPUT,
                    claim.remaining,
                )
                return self._record_outcome(span, outcome)

            span.set_attribute("derp.inline.output_chars", len(output.text))
            outcome = InlineChatCompleted(output.text, claim.remaining)
            return self._record_outcome(span, outcome)

    async def _run_provider(
        self,
        request: PreparedInlineChatRequest,
    ) -> Succeeded[TextOutput] | InlineChatFailureReason:
        try:
            async with asyncio.timeout(self._policy.provider_deadline_seconds):
                outcome = await self._executor.answer(INLINE_CHAT_PLAN, request)
        except TimeoutError:
            return InlineChatFailureReason.PROVIDER_TIMEOUT
        except Exception as exc:
            report_exception(
                "inline_chat_provider_failed",
                exception=exc,
                level="warning",
                model_key=INLINE_CHAT_PLAN.model.key.value,
            )
            return InlineChatFailureReason.PROVIDER_ERROR
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

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("inline chat clock must be timezone-aware")
        return now

    @staticmethod
    def _record_outcome(
        span: logfire.LogfireSpan,
        outcome: InlineChatOutcome,
    ) -> InlineChatOutcome:
        if isinstance(outcome, InlineChatCompleted):
            category = "completed"
            remaining = outcome.allowance_remaining
        elif isinstance(outcome, InlineChatExhausted):
            category = "allowance_exhausted"
            remaining = 0
        elif isinstance(outcome, InlineChatInvalid):
            category = "invalid"
            remaining = -1
        elif isinstance(outcome, InlineChatFailed):
            category = f"failed:{outcome.reason.value}"
            remaining = outcome.allowance_remaining
        else:  # pragma: no cover - closed outcome union
            raise TypeError(f"unsupported inline outcome: {type(outcome).__name__}")
        span.set_attribute("derp.inline.outcome", category)
        if remaining is not None and remaining >= 0:
            span.set_attribute("derp.inline.allowance_remaining", remaining)
        return outcome


def _validate_allowance_count(used_count: int, limit: int) -> None:
    for name, value in (("used_count", used_count), ("limit", limit)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
    if used_count < 0:
        raise ValueError("used_count must not be negative")
    if limit <= 0:
        raise ValueError("limit must be positive")


def _validate_nonnegative_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _validate_positive_count(name: str, value: int) -> None:
    _validate_nonnegative_count(name, value)
    if value == 0:
        raise ValueError(f"{name} must be positive")


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DEFAULT_INLINE_DAILY_REQUESTS",
    "INLINE_CHAT_PLAN",
    "MAX_INLINE_DAILY_REQUESTS",
    "MAX_INLINE_OUTPUT_BYTES",
    "MAX_INLINE_OUTPUT_CHARS",
    "MAX_INLINE_OUTPUT_TOKENS",
    "MAX_INLINE_QUERY_BYTES",
    "MAX_INLINE_QUERY_CHARS",
    "InlineAllowanceClaim",
    "InlineAllowanceClaimer",
    "InlineAllowanceExhausted",
    "InlineAllowanceGranted",
    "InlineChatCompleted",
    "InlineChatExhausted",
    "InlineChatFailed",
    "InlineChatFailureReason",
    "InlineChatFeatureService",
    "InlineChatInvalid",
    "InlineChatOutcome",
    "InlineChatPolicy",
    "InlineChatProviderExecutor",
    "InlineChatRequest",
    "PreparedInlineChatRequest",
]
