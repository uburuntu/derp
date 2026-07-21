"""Bounded inline chat policy and typed outcome tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire

from derp.execution import Failed, FailureReason, Rejected, RejectionReason, Succeeded
from derp.features.inline_chat import (
    INLINE_CHAT_PLAN,
    MAX_INLINE_OUTPUT_CHARS,
    MAX_INLINE_QUERY_CHARS,
    InlineAllowanceExhausted,
    InlineAllowanceGranted,
    InlineChatCompleted,
    InlineChatExhausted,
    InlineChatFailed,
    InlineChatFailureReason,
    InlineChatFeatureService,
    InlineChatInvalid,
    InlineChatPolicy,
)
from derp.features.types import TextOutput

USER_ID = UUID("f43dff23-a61d-4f06-aada-56f580fe2ddb")
NOW = datetime(2026, 7, 21, 23, 59, tzinfo=UTC)
TODAY = date(2026, 7, 21)


@dataclass(slots=True)
class RecordingAllowance:
    result: object
    active: bool = False
    calls: int = 0

    async def claim(self, **_kwargs):
        self.calls += 1
        self.active = True
        await asyncio.sleep(0)
        self.active = False
        return self.result


@dataclass(slots=True)
class RecordingExecutor:
    result: object
    allowance: RecordingAllowance
    calls: int = 0
    request: object | None = None

    async def answer(self, plan, request):
        assert plan is INLINE_CHAT_PLAN
        assert not self.allowance.active
        self.calls += 1
        self.request = request
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _service(
    provider_outcome: object = Succeeded(TextOutput("bounded answer")),
    *,
    allowance_outcome: object = InlineAllowanceGranted(TODAY, 1, 10),
    policy: InlineChatPolicy | None = None,
) -> tuple[InlineChatFeatureService, RecordingAllowance, RecordingExecutor]:
    allowance = RecordingAllowance(allowance_outcome)
    executor = RecordingExecutor(provider_outcome, allowance)
    return (
        InlineChatFeatureService(
            allowance,
            executor,
            policy=policy,
            clock=lambda: NOW,
        ),
        allowance,
        executor,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["", "   ", "x" * (MAX_INLINE_QUERY_CHARS + 1), "contains\x00nul"],
)
async def test_invalid_query_never_claims_or_calls_provider(query: str) -> None:
    service, allowance, executor = _service()

    outcome = await service.answer(user_id=USER_ID, query=query)

    assert isinstance(outcome, InlineChatInvalid)
    assert allowance.calls == executor.calls == 0


@pytest.mark.asyncio
async def test_exhausted_day_never_calls_provider_and_reports_utc_reset() -> None:
    service, allowance, executor = _service(
        allowance_outcome=InlineAllowanceExhausted(TODAY, 10, 10)
    )

    outcome = await service.answer(user_id=USER_ID, query="answer this")

    assert outcome == InlineChatExhausted(
        datetime(2026, 7, 22, tzinfo=UTC),
        10,
    )
    assert allowance.calls == 1
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_provider_runs_once_after_allowance_transaction_closes() -> None:
    service, allowance, executor = _service()

    outcome = await service.answer(user_id=USER_ID, query="  direct query only  ")

    assert outcome == InlineChatCompleted("bounded answer", 9)
    assert allowance.calls == executor.calls == 1
    assert executor.request.query == "direct query only"
    assert executor.request.max_output_tokens == 256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "reason"),
    [
        (Rejected(RejectionReason.POLICY), InlineChatFailureReason.PROVIDER_REJECTED),
        (
            Rejected(RejectionReason.UNUSABLE_OUTPUT),
            InlineChatFailureReason.UNUSABLE_OUTPUT,
        ),
        (Failed(FailureReason.PROVIDER_ERROR), InlineChatFailureReason.PROVIDER_ERROR),
        (
            RuntimeError("private provider detail"),
            InlineChatFailureReason.PROVIDER_ERROR,
        ),
    ],
)
async def test_provider_failures_are_typed_and_content_free(
    provider_outcome: object,
    reason: InlineChatFailureReason,
) -> None:
    service, _, _ = _service(provider_outcome)

    outcome = await service.answer(user_id=USER_ID, query="private query")

    assert outcome == InlineChatFailed(reason, 9)
    assert "private query" not in repr(outcome)
    assert "private provider detail" not in repr(outcome)


@pytest.mark.asyncio
async def test_timeout_cancels_provider_and_returns_typed_failure() -> None:
    allowance = RecordingAllowance(InlineAllowanceGranted(TODAY, 1, 10))
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def wait_forever(*_args):
        provider_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            provider_cancelled.set()

    executor = AsyncMock()
    executor.answer.side_effect = wait_forever
    service = InlineChatFeatureService(
        allowance,
        executor,
        policy=InlineChatPolicy(provider_deadline_seconds=0.01),
        clock=lambda: NOW,
    )

    outcome = await service.answer(user_id=USER_ID, query="bounded timeout")

    assert provider_started.is_set()
    assert provider_cancelled.is_set()
    assert outcome == InlineChatFailed(InlineChatFailureReason.PROVIDER_TIMEOUT, 9)


@pytest.mark.asyncio
async def test_oversized_provider_text_is_never_exposed() -> None:
    service, _, _ = _service(Succeeded(TextOutput("x" * (MAX_INLINE_OUTPUT_CHARS + 1))))

    outcome = await service.answer(user_id=USER_ID, query="bounded output")

    assert outcome == InlineChatFailed(InlineChatFailureReason.UNUSABLE_OUTPUT, 9)


@pytest.mark.asyncio
async def test_telemetry_contains_sizes_and_outcome_but_not_content(
    capfire: CaptureLogfire,
) -> None:
    capfire.exporter.clear()
    service, _, _ = _service(Succeeded(TextOutput("private-output-sentinel")))

    outcome = await service.answer(
        user_id=USER_ID,
        query="private-query-sentinel",
    )

    assert isinstance(outcome, InlineChatCompleted)
    spans = capfire.exporter.exported_spans_as_dict()
    serialized = json.dumps(spans)
    assert "private-query-sentinel" not in serialized
    assert "private-output-sentinel" not in serialized
    span = next(item for item in spans if item["name"] == "inline_chat.operation")
    assert span["attributes"]["derp.inline.outcome"] == "completed"
    assert span["attributes"]["derp.inline.query_chars"] == len(
        "private-query-sentinel"
    )
    assert span["attributes"]["derp.inline.output_chars"] == len(
        "private-output-sentinel"
    )
