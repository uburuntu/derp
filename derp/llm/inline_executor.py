"""Pydantic AI adapter for one bounded inline answer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.google import GoogleModelSettings

from derp.catalog import GoogleModelKey
from derp.execution import (
    ExecutionPlan,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.inline_chat import PreparedInlineChatRequest
from derp.features.types import TextOutput
from derp.llm.agents import create_inline_agent


class InlineAgentRunResult(Protocol):
    """Small result surface required from Pydantic AI."""

    output: str


class InlineAgent(Protocol):
    """One-request Pydantic AI surface used by the provider adapter."""

    async def run(
        self,
        prompt: str,
        /,
        *,
        model_settings: GoogleModelSettings,
        usage_limits: UsageLimits,
    ) -> InlineAgentRunResult: ...


type InlineAgentFactory = Callable[[ExecutionPlan], InlineAgent]


class PydanticAIInlineExecutor:
    """Run one economy-model request without Telegram or persistence effects."""

    def __init__(
        self,
        agent_factory: InlineAgentFactory = create_inline_agent,
    ) -> None:
        if not callable(agent_factory):
            raise TypeError("agent_factory must be callable")
        self._agent_factory = agent_factory

    async def answer(
        self,
        plan: ExecutionPlan,
        request: PreparedInlineChatRequest,
    ) -> Outcome[TextOutput]:
        """Submit only the normalized query under one-request usage limits."""
        if plan.feature is not Feature.INLINE_CHAT:
            raise ValueError(f"{plan.feature.value} cannot execute inline chat")
        if plan.model.key is not GoogleModelKey.CHAT_ECONOMY:
            raise ValueError("free inline chat requires the economy model")
        if not isinstance(request, PreparedInlineChatRequest):
            raise TypeError("request must be a PreparedInlineChatRequest")

        agent = self._agent_factory(plan)
        try:
            result = await agent.run(
                request.query,
                model_settings=GoogleModelSettings(
                    max_tokens=request.max_output_tokens,
                    temperature=0.2,
                ),
                usage_limits=UsageLimits(
                    request_limit=1,
                    input_tokens_limit=request.input_tokens_limit,
                    output_tokens_limit=request.max_output_tokens,
                    total_tokens_limit=(
                        request.input_tokens_limit + request.max_output_tokens
                    ),
                ),
            )
        except UsageLimitExceeded:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        if not isinstance(result.output, str):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        try:
            return Succeeded(TextOutput(result.output))
        except ValueError:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)


__all__ = [
    "InlineAgent",
    "InlineAgentFactory",
    "InlineAgentRunResult",
    "PydanticAIInlineExecutor",
]
