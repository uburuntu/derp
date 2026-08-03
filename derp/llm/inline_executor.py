"""Pydantic AI adapter for one bounded inline answer."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol

from pydantic_ai import ModelMessage, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import ModelSettings

from derp.catalog import ModelRole
from derp.execution import (
    ExecutionPlan,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.inline_chat import (
    InlineProviderExecution,
    PreparedInlineChatRequest,
)
from derp.features.types import TextOutput
from derp.inference.report import reports_from_messages
from derp.llm.agents import create_inline_agent
from derp.llm.providers import model_run_settings


class InlineAgentRunResult(Protocol):
    """Small result surface required from Pydantic AI."""

    output: str

    def new_messages(self) -> list[ModelMessage]: ...


class InlineAgent(Protocol):
    """One-request Pydantic AI surface used by the provider adapter."""

    async def run(
        self,
        prompt: str,
        /,
        *,
        model_settings: ModelSettings,
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
        *,
        user_id: uuid.UUID,
    ) -> InlineProviderExecution:
        """Submit only the normalized query under one-request usage limits."""
        if plan.feature is not Feature.INLINE_CHAT:
            raise ValueError(f"{plan.feature.value} cannot execute inline chat")
        if plan.model.key not in {ModelRole.CHAT_ECONOMY, ModelRole.FREE_TEXT}:
            raise ValueError("inline chat requires an economy or free-text model")
        if not isinstance(request, PreparedInlineChatRequest):
            raise TypeError("request must be a PreparedInlineChatRequest")
        if not isinstance(user_id, uuid.UUID):
            raise TypeError("user_id must be a UUID")

        agent = self._agent_factory(plan)
        settings = model_run_settings(plan.model, user_id=user_id).copy()
        settings.update(
            max_tokens=request.max_output_tokens,
            temperature=0.2,
        )
        try:
            result = await agent.run(
                request.query,
                model_settings=settings,
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
            return InlineProviderExecution(
                Rejected(RejectionReason.UNUSABLE_OUTPUT),
                provider_completed=False,
            )

        reports = reports_from_messages(
            result.new_messages(),
            requested_model=plan.model.provider_model_id,
        )
        if not isinstance(result.output, str):
            return InlineProviderExecution(
                Rejected(RejectionReason.UNUSABLE_OUTPUT),
                reports,
            )
        try:
            outcome: Outcome[TextOutput] = Succeeded(TextOutput(result.output))
        except ValueError:
            outcome = Rejected(RejectionReason.UNUSABLE_OUTPUT)
        return InlineProviderExecution(outcome, reports)


__all__ = [
    "InlineAgent",
    "InlineAgentFactory",
    "InlineAgentRunResult",
    "PydanticAIInlineExecutor",
]
