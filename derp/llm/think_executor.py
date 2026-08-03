"""Pydantic AI adapter for provider-neutral deep reasoning."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking
from pydantic_ai.models.google import GoogleModelSettings

from derp.execution import (
    ExecutionPlan,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.think import PreparedThinkRequest
from derp.features.types import TextOutput
from derp.llm.providers import create_model

THINK_INSTRUCTIONS = """You are Derp's deliberate reasoning mode.
Analyze the problem carefully and privately. Return a direct, rigorous answer with the
important assumptions, checkable derivations, conclusions, and material uncertainty.
Do not reveal hidden chain-of-thought or invent sources."""


class ThinkAgentRunResult(Protocol):
    """Small result surface required from Pydantic AI."""

    output: str


class ThinkAgent(Protocol):
    """Small agent surface used by the provider adapter."""

    async def run(self, prompt: str, /) -> ThinkAgentRunResult: ...


type ThinkAgentFactory = Callable[[ExecutionPlan, int], ThinkAgent]


def _create_think_agent(
    plan: ExecutionPlan,
    max_output_tokens: int,
) -> ThinkAgent:
    if plan.feature is not Feature.DEEP_THINK:
        raise ValueError(f"{plan.feature.value} is not a deep reasoning plan")
    agent: Agent[object, str] = Agent(
        create_model(plan.model),
        name="deep_think",
        output_type=str,
        instructions=THINK_INSTRUCTIONS,
        model_settings=GoogleModelSettings(
            max_tokens=max_output_tokens,
            temperature=0.2,
        ),
        capabilities=[Thinking(effort="high")],
    )
    return agent


class PydanticAIThinkExecutor:
    """Run the selected reasoning model without Telegram or billing effects."""

    def __init__(self, agent_factory: ThinkAgentFactory = _create_think_agent) -> None:
        self._agent_factory = agent_factory

    async def reason(
        self,
        plan: ExecutionPlan,
        request: PreparedThinkRequest,
    ) -> Outcome[TextOutput]:
        """Execute the exact plan and normalize its text result."""
        agent = self._agent_factory(plan, request.max_output_tokens)
        result = await agent.run(request.problem)
        if not isinstance(result.output, str):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        try:
            return Succeeded(TextOutput(result.output))
        except ValueError:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)


__all__ = [
    "PydanticAIThinkExecutor",
    "THINK_INSTRUCTIONS",
    "ThinkAgent",
    "ThinkAgentFactory",
    "ThinkAgentRunResult",
]
