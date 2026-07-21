"""Contracts for bounded provider-neutral deep reasoning."""

from __future__ import annotations

import asyncio
import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from derp.features.think import (
    MAX_THINK_OUTPUT_CHARS,
    MAX_THINK_OUTPUT_TOKENS,
    MAX_THINK_PROBLEM_CHARS,
    PreparedThinkRequest,
    ThinkExecutionPolicy,
    ThinkFeatureService,
    ThinkRequest,
)
from derp.features.types import TextOutput
from derp.operations import DEFAULT_QUOTE_POLICY


def test_think_request_is_normalized_frozen_and_bounded() -> None:
    request = ThinkRequest("  verify this proof  ")

    assert request.problem == "verify this proof"
    with pytest.raises(FrozenInstanceError):
        request.problem = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="must not be blank"):
        ThinkRequest("  ")
    with pytest.raises(ValueError, match="at most"):
        ThinkRequest("x" * (MAX_THINK_PROBLEM_CHARS + 1))


def test_execution_output_ceiling_matches_the_fixed_quote_policy() -> None:
    assert MAX_THINK_OUTPUT_TOKENS == DEFAULT_QUOTE_POLICY.deep_think_output_tokens


def test_prepared_request_enforces_provider_boundary_invariants() -> None:
    request = PreparedThinkRequest("  Question  ", MAX_THINK_OUTPUT_TOKENS)

    assert request.problem == "Question"
    with pytest.raises(ValueError, match="must not exceed"):
        PreparedThinkRequest("Question", MAX_THINK_OUTPUT_TOKENS + 1)


@pytest.mark.asyncio
async def test_service_passes_exact_plan_and_owned_output_limit() -> None:
    plan = plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING)
    output = Succeeded(TextOutput("A checkable answer"))
    executor = SimpleNamespace(reason=AsyncMock(return_value=output))

    result = await ThinkFeatureService(executor).reason(
        plan,
        ThinkRequest("Question"),
    )

    assert result is output
    prepared = executor.reason.await_args.args[1]
    assert executor.reason.await_args.args[0] is plan
    assert prepared == PreparedThinkRequest("Question", MAX_THINK_OUTPUT_TOKENS)


@pytest.mark.asyncio
async def test_service_rejects_a_plan_for_another_feature() -> None:
    executor = SimpleNamespace(reason=AsyncMock())

    with pytest.raises(ValueError, match="cannot execute deep_think"):
        await ThinkFeatureService(executor).reason(
            plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD),
            ThinkRequest("Question"),
        )

    executor.reason.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_deadline_cancels_execution_and_returns_typed_failure() -> None:
    class SlowExecutor:
        def __init__(self) -> None:
            self.cancelled = False

        async def reason(self, plan, request):
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled = True

    executor = SlowExecutor()
    service = ThinkFeatureService(
        executor,
        policy=ThinkExecutionPolicy(provider_deadline_seconds=0.001),
    )

    result = await service.reason(
        plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING),
        ThinkRequest("Question"),
    )

    assert result == Failed(FailureReason.PROVIDER_ERROR)
    assert executor.cancelled


@pytest.mark.asyncio
async def test_provider_exception_and_typed_rejection_are_contained() -> None:
    plan = plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING)
    rejected = Rejected(RejectionReason.POLICY)
    executor = SimpleNamespace(reason=AsyncMock(return_value=rejected))
    service = ThinkFeatureService(executor)

    assert await service.reason(plan, ThinkRequest("Question")) is rejected
    executor.reason.side_effect = RuntimeError("private provider detail")
    assert await service.reason(plan, ThinkRequest("Question")) == Failed(
        FailureReason.PROVIDER_ERROR
    )


@pytest.mark.asyncio
async def test_unusable_or_oversized_output_is_rejected() -> None:
    plan = plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING)
    executor = SimpleNamespace(reason=AsyncMock(return_value=Succeeded(object())))
    service = ThinkFeatureService(executor)

    assert await service.reason(plan, ThinkRequest("Question")) == Rejected(
        RejectionReason.UNUSABLE_OUTPUT
    )
    executor.reason.return_value = Succeeded(TextOutput("x" * 4))
    limited = ThinkFeatureService(
        executor,
        policy=ThinkExecutionPolicy(max_output_chars=3),
    )
    assert await limited.reason(plan, ThinkRequest("Question")) == Rejected(
        RejectionReason.UNUSABLE_OUTPUT
    )


def test_policy_rejects_unbounded_configuration() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ThinkExecutionPolicy(provider_deadline_seconds=math.inf)
    with pytest.raises(ValueError, match="must not exceed"):
        ThinkExecutionPolicy(max_output_tokens=MAX_THINK_OUTPUT_TOKENS + 1)
    with pytest.raises(ValueError, match="must not exceed"):
        ThinkExecutionPolicy(max_output_chars=MAX_THINK_OUTPUT_CHARS + 1)
    with pytest.raises(TypeError, match="must be an integer"):
        ThinkExecutionPolicy(max_output_tokens=1.5)  # type: ignore[arg-type]
