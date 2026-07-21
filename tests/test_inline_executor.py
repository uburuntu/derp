"""Pydantic AI inline adapter request-boundary tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

from derp.catalog import GoogleModelKey
from derp.execution import (
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.inline_chat import INLINE_CHAT_PLAN, PreparedInlineChatRequest
from derp.features.types import TextOutput
from derp.llm.inline_executor import PydanticAIInlineExecutor


def _request() -> PreparedInlineChatRequest:
    return PreparedInlineChatRequest(
        "What is a savepoint?",
        max_output_tokens=128,
        input_tokens_limit=512,
    )


@pytest.mark.asyncio
async def test_executor_sends_only_query_in_exactly_one_bounded_run() -> None:
    agent = MagicMock()
    agent.run = AsyncMock(return_value=SimpleNamespace(output="A nested transaction."))
    factory = MagicMock(return_value=agent)

    outcome = await PydanticAIInlineExecutor(factory).answer(
        INLINE_CHAT_PLAN,
        _request(),
    )

    assert outcome == Succeeded(TextOutput("A nested transaction."))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "   ", object()])
async def test_executor_rejects_unusable_output(output: object) -> None:
    agent = MagicMock()
    agent.run = AsyncMock(return_value=SimpleNamespace(output=output))

    outcome = await PydanticAIInlineExecutor(lambda _plan: agent).answer(
        INLINE_CHAT_PLAN,
        _request(),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)


@pytest.mark.asyncio
async def test_usage_limit_failure_is_a_private_typed_rejection() -> None:
    agent = MagicMock()
    agent.run = AsyncMock(
        side_effect=UsageLimitExceeded("private token counts must not escape")
    )

    outcome = await PydanticAIInlineExecutor(lambda _plan: agent).answer(
        INLINE_CHAT_PLAN,
        _request(),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    assert "private" not in repr(outcome)


@pytest.mark.asyncio
async def test_executor_rejects_non_inline_or_non_economy_plans() -> None:
    executor = PydanticAIInlineExecutor(MagicMock())

    with pytest.raises(ValueError, match="cannot execute inline"):
        await executor.answer(
            plan_execution(Feature.CHAT, GoogleModelKey.CHAT_ECONOMY),
            _request(),
        )
    with pytest.raises(ValueError, match="economy model"):
        await executor.answer(
            plan_execution(Feature.INLINE_CHAT, GoogleModelKey.CHAT_STANDARD),
            _request(),
        )
