"""Provider-neutral deep-reasoning requests and bounded execution service."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from derp.execution import (
    ExecutionPlan,
    Failed,
    FailureReason,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.types import TextOutput

MAX_THINK_PROBLEM_CHARS = 32_000
MAX_THINK_OUTPUT_CHARS = 64_000
MAX_THINK_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class ThinkRequest:
    """Validated problem for one deliberate reasoning run."""

    problem: str

    def __post_init__(self) -> None:
        normalized = self.problem.strip()
        if not normalized:
            raise ValueError("reasoning problem must not be blank")
        if len(normalized) > MAX_THINK_PROBLEM_CHARS:
            raise ValueError(
                f"reasoning problem must be at most {MAX_THINK_PROBLEM_CHARS} characters"
            )
        object.__setattr__(self, "problem", normalized)


@dataclass(frozen=True, slots=True)
class PreparedThinkRequest:
    """Reasoning request with the execution-owned output ceiling."""

    problem: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        normalized = self.problem.strip()
        if not normalized:
            raise ValueError("prepared reasoning problem must not be blank")
        if len(normalized) > MAX_THINK_PROBLEM_CHARS:
            raise ValueError(
                f"prepared reasoning problem must be at most "
                f"{MAX_THINK_PROBLEM_CHARS} characters"
            )
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if self.max_output_tokens > MAX_THINK_OUTPUT_TOKENS:
            raise ValueError(
                f"max_output_tokens must not exceed {MAX_THINK_OUTPUT_TOKENS}"
            )
        object.__setattr__(self, "problem", normalized)


@dataclass(frozen=True, slots=True)
class ThinkExecutionPolicy:
    """Deadline and output limits enforced around the reasoning provider."""

    provider_deadline_seconds: float = 120.0
    max_output_tokens: int = MAX_THINK_OUTPUT_TOKENS
    max_output_chars: int = MAX_THINK_OUTPUT_CHARS

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.provider_deadline_seconds)
            or self.provider_deadline_seconds <= 0
        ):
            raise ValueError("provider_deadline_seconds must be finite and positive")
        for name in ("max_output_tokens", "max_output_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_output_tokens > MAX_THINK_OUTPUT_TOKENS:
            raise ValueError(
                f"max_output_tokens must not exceed {MAX_THINK_OUTPUT_TOKENS}"
            )
        if self.max_output_chars > MAX_THINK_OUTPUT_CHARS:
            raise ValueError(
                f"max_output_chars must not exceed {MAX_THINK_OUTPUT_CHARS}"
            )


class ThinkProviderExecutor(Protocol):
    """Provider adapter with no Telegram, persistence, or billing effects."""

    async def reason(
        self,
        plan: ExecutionPlan,
        request: PreparedThinkRequest,
    ) -> Outcome[TextOutput]:
        """Return one provider-independent reasoning result."""
        ...


class ThinkFeatureService:
    """Execute deep reasoning behind typed limits and failure behavior."""

    def __init__(
        self,
        executor: ThinkProviderExecutor,
        *,
        policy: ThinkExecutionPolicy | None = None,
    ) -> None:
        self._executor = executor
        self._policy = policy or ThinkExecutionPolicy()

    async def reason(
        self,
        plan: ExecutionPlan,
        request: ThinkRequest,
    ) -> Outcome[TextOutput]:
        """Reason within the selected model's output limit and hard deadline."""
        if plan.feature is not Feature.DEEP_THINK:
            raise ValueError(f"{plan.feature.value} cannot execute deep_think")
        model_limit = plan.model.output_token_limit
        if model_limit is not None and self._policy.max_output_tokens > model_limit:
            raise ValueError("reasoning output policy exceeds the model output limit")
        prepared = PreparedThinkRequest(
            problem=request.problem,
            max_output_tokens=self._policy.max_output_tokens,
        )
        return await self._run_provider(self._executor.reason(plan, prepared))

    async def _run_provider(
        self,
        execution: Awaitable[Outcome[TextOutput]],
    ) -> Outcome[TextOutput]:
        try:
            async with asyncio.timeout(self._policy.provider_deadline_seconds):
                outcome = await execution
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)
        if isinstance(outcome, (Rejected, Failed)):
            return outcome
        if not isinstance(outcome, Succeeded) or not isinstance(
            outcome.value, TextOutput
        ):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if len(outcome.value.text) > self._policy.max_output_chars:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        return outcome


__all__ = [
    "MAX_THINK_OUTPUT_CHARS",
    "MAX_THINK_OUTPUT_TOKENS",
    "MAX_THINK_PROBLEM_CHARS",
    "PreparedThinkRequest",
    "ThinkExecutionPolicy",
    "ThinkFeatureService",
    "ThinkProviderExecutor",
    "ThinkRequest",
]
