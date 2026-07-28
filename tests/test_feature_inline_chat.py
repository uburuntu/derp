"""Unlimited consented inline inference with bounded per-request execution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from derp.execution import Rejected, RejectionReason, Succeeded
from derp.features.inline_chat import (
    FREE_INLINE_CHAT_PLAN,
    MAX_INLINE_OUTPUT_CHARS,
    MAX_INLINE_QUERY_CHARS,
    InlineChatCompleted,
    InlineChatFailed,
    InlineChatFailureReason,
    InlineChatFeatureService,
    InlineChatInvalid,
    InlineChatInvocation,
    InlineChatPolicy,
    InlineProviderExecution,
)
from derp.features.types import TextOutput
from derp.inference import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    InferenceAttempt,
    InferencePrivacyMode,
    InferencePrivacyPreference,
    InferenceRecorder,
)
from derp.inference_types import InferenceReport
from derp.inference_usage import InferenceTokenUsage, InferenceUsageId

USER_ID = UUID("f43dff23-a61d-4f06-aada-56f580fe2ddb")
REQUEST_ID = UUID("94f55bc6-a0c9-4867-bc71-a5bfa2ae442a")
ATTEMPT_ID = UUID("dd9fceef-16a3-45d7-8498-435d163bd908")
NOW = datetime(2026, 7, 21, 23, 59, tzinfo=UTC)


def _privacy(*, consented: bool) -> InferencePrivacyPreference:
    if not consented:
        return InferencePrivacyPreference()
    return InferencePrivacyPreference(
        mode=InferencePrivacyMode.ALLOW_NON_ZDR_FREE,
        revision=2,
        accepted_tos_version=FREE_INFERENCE_TOS_VERSION,
        accepted_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        accepted_at=NOW,
    )


def _invocation(query: str, *, consented: bool = True) -> InlineChatInvocation:
    return InlineChatInvocation(
        REQUEST_ID, USER_ID, query, _privacy(consented=consented)
    )


def _service(
    execution: InlineProviderExecution | BaseException | None = None,
    *,
    free_plan=FREE_INLINE_CHAT_PLAN,
    policy: InlineChatPolicy | None = None,
) -> tuple[InlineChatFeatureService, AsyncMock, AsyncMock]:
    executor = AsyncMock()
    executor.answer.side_effect = (
        execution if isinstance(execution, BaseException) else None
    )
    if not isinstance(execution, BaseException):
        executor.answer.return_value = execution or InlineProviderExecution(
            Succeeded(TextOutput("bounded answer"))
        )
    recorder = AsyncMock(spec=InferenceRecorder)
    recorder.start.return_value = InferenceAttempt(
        InferenceUsageId(ATTEMPT_ID), FREE_INLINE_CHAT_PLAN.model
    )
    service = InlineChatFeatureService(
        executor,
        recorder,
        free_plan=free_plan,
        policy=policy,
    )
    return service, executor, recorder


@pytest.mark.asyncio
async def test_consent_is_required_without_subsidized_private_fallback() -> None:
    service, executor, recorder = _service()

    outcome = await service.answer(_invocation("private please", consented=False))

    assert outcome == InlineChatFailed(InlineChatFailureReason.FREE_MODE_REQUIRED)
    executor.answer.assert_not_awaited()
    recorder.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_consented_free_model_has_no_daily_admission_limit() -> None:
    service, executor, recorder = _service()

    outcomes = [await service.answer(_invocation("answer")) for _ in range(3)]

    assert outcomes == [InlineChatCompleted("bounded answer")] * 3
    assert executor.answer.await_count == 3
    assert recorder.start.await_count == 3
    assert recorder.succeed_reports.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["", "   ", "x" * (MAX_INLINE_QUERY_CHARS + 1), "contains\x00nul"],
)
async def test_invalid_query_never_calls_provider(query: str) -> None:
    service, executor, recorder = _service()

    assert isinstance(await service.answer(_invocation(query)), InlineChatInvalid)
    executor.answer.assert_not_awaited()
    recorder.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_report_is_recorded() -> None:
    report = InferenceReport(
        provider="openrouter",
        requested_model=FREE_INLINE_CHAT_PLAN.model.provider_model_id,
        actual_model=FREE_INLINE_CHAT_PLAN.model.provider_model_id,
        downstream_provider="nvidia",
        provider_response_id="gen-inline",
        generation_id="gen-inline",
        finish_reason="stop",
        tokens=InferenceTokenUsage(input_tokens=12, output_tokens=7, total_tokens=19),
        actual_cost_usd=Decimal("0"),
    )
    service, _, recorder = _service(
        InlineProviderExecution(Succeeded(TextOutput("accounted")), (report,))
    )

    assert await service.answer(_invocation("account")) == InlineChatCompleted(
        "accounted"
    )
    recorder.succeed_reports.assert_awaited_once()
    assert recorder.succeed_reports.await_args.args[1] == (report,)


@pytest.mark.asyncio
async def test_accounting_failure_prevents_untracked_provider_call() -> None:
    service, executor, recorder = _service()
    recorder.start.side_effect = RuntimeError("database unavailable")

    outcome = await service.answer(_invocation("answer"))

    assert outcome == InlineChatFailed(InlineChatFailureReason.ACCOUNTING_UNAVAILABLE)
    executor.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_timeout_is_bounded_and_recorded_failed() -> None:
    started = asyncio.Event()

    async def wait_forever(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    service, executor, recorder = _service(
        policy=InlineChatPolicy(provider_deadline_seconds=0.01)
    )
    executor.answer.side_effect = wait_forever

    outcome = await service.answer(_invocation("answer"))

    assert started.is_set()
    assert outcome == InlineChatFailed(InlineChatFailureReason.PROVIDER_TIMEOUT)
    recorder.fail.assert_awaited_once()


@pytest.mark.asyncio
async def test_oversized_or_rejected_output_is_not_exposed() -> None:
    oversized, _, _ = _service(
        InlineProviderExecution(
            Succeeded(TextOutput("x" * (MAX_INLINE_OUTPUT_CHARS + 1)))
        )
    )
    rejected, _, _ = _service(InlineProviderExecution(Rejected(RejectionReason.POLICY)))

    assert await oversized.answer(_invocation("answer")) == InlineChatFailed(
        InlineChatFailureReason.UNUSABLE_OUTPUT
    )
    assert await rejected.answer(_invocation("answer")) == InlineChatFailed(
        InlineChatFailureReason.PROVIDER_REJECTED
    )
