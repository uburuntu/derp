"""Pydantic AI inline adapter request-boundary tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic_ai import ModelResponse, RequestUsage, TextPart
from pydantic_ai.exceptions import UsageLimitExceeded

from derp.catalog import GoogleModelKey
from derp.execution import (
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.inline_chat import (
    FREE_INLINE_CHAT_PLAN,
    INLINE_CHAT_PLAN,
    PreparedInlineChatRequest,
)
from derp.features.types import TextOutput
from derp.llm.inline_executor import PydanticAIInlineExecutor

USER_ID = UUID("bbd34589-1248-4f86-bb95-c75f026c67db")


def _request() -> PreparedInlineChatRequest:
    return PreparedInlineChatRequest(
        "What is a savepoint?",
        max_output_tokens=128,
        input_tokens_limit=512,
    )


@pytest.mark.asyncio
async def test_executor_sends_only_query_in_exactly_one_bounded_run() -> None:
    agent = MagicMock()
    agent.run = AsyncMock(
        return_value=SimpleNamespace(
            output="A nested transaction.",
            new_messages=lambda: [],
        )
    )
    factory = MagicMock(return_value=agent)

    outcome = await PydanticAIInlineExecutor(factory).answer(
        INLINE_CHAT_PLAN,
        _request(),
        user_id=USER_ID,
    )

    assert outcome.outcome == Succeeded(TextOutput("A nested transaction."))
    assert outcome.reports == ()
    assert outcome.provider_completed
    assert "A nested transaction." not in repr(outcome)
    factory.assert_called_once_with(INLINE_CHAT_PLAN)
    agent.run.assert_awaited_once()
    prompt = agent.run.await_args.args[0]
    assert prompt == "What is a savepoint?"
    assert "User:" not in prompt
    limits = agent.run.await_args.kwargs["usage_limits"]
    assert limits.request_limit == 1
    assert limits.input_tokens_limit == 512
    assert limits.output_tokens_limit == 128
    assert limits.total_tokens_limit == 640
    settings = agent.run.await_args.kwargs["model_settings"]
    assert settings["max_tokens"] == 128
    assert settings["temperature"] == 0.2
    assert settings["openrouter_provider"]["zdr"] is True
    assert settings["openrouter_usage"] == {"include": True}
    assert settings["openai_user"].startswith("derp_")


@pytest.mark.asyncio
async def test_executor_preserves_content_free_provider_report() -> None:
    response = ModelResponse(
        parts=[TextPart("content must not enter the report")],
        usage=RequestUsage(input_tokens=14, output_tokens=5),
        model_name=INLINE_CHAT_PLAN.model.provider_model_id,
        provider_name="openrouter",
        provider_response_id="gen-inline-1",
        provider_details={"cost": 0},
    )
    agent = MagicMock()
    agent.run = AsyncMock(
        return_value=SimpleNamespace(
            output="A bounded answer.",
            new_messages=lambda: [response],
        )
    )

    execution = await PydanticAIInlineExecutor(lambda _plan: agent).answer(
        INLINE_CHAT_PLAN,
        _request(),
        user_id=USER_ID,
    )

    assert len(execution.reports) == 1
    report = execution.reports[0]
    assert report.tokens is not None
    assert report.tokens.input_tokens == 14
    assert report.tokens.output_tokens == 5
    assert report.provider_response_id == "gen-inline-1"
    assert "content must not enter the report" not in repr(report)


@pytest.mark.asyncio
async def test_free_plan_applies_explicit_non_zdr_zero_price_route() -> None:
    agent = MagicMock()
    agent.run = AsyncMock(
        return_value=SimpleNamespace(output="Free answer.", new_messages=lambda: [])
    )

    await PydanticAIInlineExecutor(lambda _plan: agent).answer(
        FREE_INLINE_CHAT_PLAN,
        _request(),
        user_id=USER_ID,
    )

    settings = agent.run.await_args.kwargs["model_settings"]
    route = settings["openrouter_provider"]
    assert route["data_collection"] == "allow"
    assert route["zdr"] is False
    assert route["max_price"]["prompt"] == 0.0
    assert route["max_price"]["completion"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "   ", object()])
async def test_executor_rejects_unusable_output(output: object) -> None:
    agent = MagicMock()
    agent.run = AsyncMock(
        return_value=SimpleNamespace(output=output, new_messages=lambda: [])
    )

    outcome = await PydanticAIInlineExecutor(lambda _plan: agent).answer(
        INLINE_CHAT_PLAN,
        _request(),
        user_id=USER_ID,
    )

    assert outcome.outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    assert outcome.provider_completed


@pytest.mark.asyncio
async def test_usage_limit_failure_is_a_private_typed_rejection() -> None:
    agent = MagicMock()
    agent.run = AsyncMock(
        side_effect=UsageLimitExceeded("private token counts must not escape")
    )

    outcome = await PydanticAIInlineExecutor(lambda _plan: agent).answer(
        INLINE_CHAT_PLAN,
        _request(),
        user_id=USER_ID,
    )

    assert outcome.outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    assert not outcome.provider_completed
    assert "private" not in repr(outcome)


@pytest.mark.asyncio
async def test_executor_rejects_non_inline_or_non_economy_plans() -> None:
    executor = PydanticAIInlineExecutor(MagicMock())

    with pytest.raises(ValueError, match="cannot execute inline"):
        await executor.answer(
            plan_execution(Feature.CHAT, GoogleModelKey.CHAT_ECONOMY),
            _request(),
            user_id=USER_ID,
        )
    with pytest.raises(ValueError, match="economy or free-text"):
        await executor.answer(
            plan_execution(Feature.INLINE_CHAT, GoogleModelKey.CHAT_STANDARD),
            _request(),
            user_id=USER_ID,
        )
