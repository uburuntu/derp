"""Agent factory for creating Pydantic-AI agents.

Provides factory functions for different agent types:
- Chat agent: Full-featured with tools and context
- Image agent: For image generation/editing
- Inline agent: Lightweight for inline queries
"""

from __future__ import annotations

import logfire
from pydantic_ai import Agent, BinaryImage, DeferredToolRequests, RunContext
from pydantic_ai.capabilities import ProcessHistory

from derp.catalog import GoogleModelKey, GoogleModelSpec
from derp.execution import ExecutionPlan, Feature, plan_execution
from derp.history.service import process_native_history
from derp.llm.deps import AgentDeps
from derp.llm.prompts import (
    IMAGE_SYSTEM_PROMPT,
    INLINE_SYSTEM_PROMPT,
    build_chat_system_prompt,
)
from derp.llm.providers import create_image_model, create_model


def _resolve_plan(
    model: ExecutionPlan | GoogleModelSpec | GoogleModelKey,
    *,
    default_feature: Feature,
    allowed_features: frozenset[Feature],
) -> ExecutionPlan:
    """Resolve a plan and reject plans intended for a different agent kind."""
    plan = (
        model
        if isinstance(model, ExecutionPlan)
        else plan_execution(default_feature, model)
    )
    if plan.feature not in allowed_features:
        raise ValueError(f"{plan.feature.value} cannot use this agent factory")
    return plan


def create_chat_agent(
    model: ExecutionPlan
    | GoogleModelSpec
    | GoogleModelKey = GoogleModelKey.CHAT_STANDARD,
) -> Agent[AgentDeps, str | DeferredToolRequests]:
    """Create the main chat agent with tools and context.

    The chat agent is the primary agent for handling messages in chats.
    Tools are attached by the caller so all capabilities share the same policy layer.

    Args:
        model: Exact catalog model or semantic catalog key.

    Returns:
        A configured Agent instance for chat interactions.
    """
    plan = _resolve_plan(
        model,
        default_feature=Feature.CHAT,
        allowed_features=frozenset({Feature.CHAT, Feature.DEEP_THINK}),
    )
    spec = plan.model
    provider_model = create_model(spec)

    agent: Agent[AgentDeps, str | DeferredToolRequests] = Agent(
        provider_model,
        name="chat",
        deps_type=AgentDeps,
        output_type=[str, DeferredToolRequests],
        capabilities=[ProcessHistory(process_native_history)],
    )

    @agent.instructions
    def add_chat_context(ctx: RunContext[AgentDeps]) -> str:
        return build_chat_system_prompt(ctx)

    logfire.debug(
        "chat_agent_created",
        feature=plan.feature.value,
        model_key=spec.key.value,
        model=spec.provider_model_id,
    )

    return agent


def create_image_agent(
    model: ExecutionPlan | GoogleModelSpec | GoogleModelKey = GoogleModelKey.IMAGE,
) -> Agent[object, BinaryImage | str]:
    """Create an agent for image generation and editing.

    Uses the catalog image model which supports native image generation.
    Returns either a BinaryImage or text (if image generation fails/is refused).

    Returns:
        A configured Agent instance for image generation.
    """
    plan = _resolve_plan(
        model,
        default_feature=Feature.IMAGE_GENERATE,
        allowed_features=frozenset({Feature.IMAGE_GENERATE, Feature.IMAGE_EDIT}),
    )
    spec = plan.model
    provider_model = create_image_model(spec)

    agent: Agent[object, BinaryImage | str] = Agent(
        provider_model,
        name="image",
        output_type=BinaryImage | str,
        instructions=IMAGE_SYSTEM_PROMPT,
        retries=2,  # Image models may need more attempts for output validation
    )

    logfire.debug(
        "image_agent_created",
        feature=plan.feature.value,
        model_key=spec.key.value,
        model=spec.provider_model_id,
    )

    return agent


def create_inline_agent(
    model: ExecutionPlan
    | GoogleModelSpec
    | GoogleModelKey = GoogleModelKey.CHAT_ECONOMY,
) -> Agent[object, str]:
    """Create a lightweight agent for inline queries.

    Uses the economy catalog model by default for cost efficiency on high-volume
    inline queries. No tools or complex context.

    Args:
        model: Exact catalog model or semantic catalog key.

    Returns:
        A configured Agent instance for inline queries.
    """
    plan = _resolve_plan(
        model,
        default_feature=Feature.INLINE_CHAT,
        allowed_features=frozenset({Feature.INLINE_CHAT}),
    )
    spec = plan.model
    provider_model = create_model(spec)

    agent: Agent[object, str] = Agent(
        provider_model,
        name="inline",
        output_type=str,
        instructions=INLINE_SYSTEM_PROMPT,
    )

    logfire.debug(
        "inline_agent_created",
        feature=plan.feature.value,
        model_key=spec.key.value,
        model=spec.provider_model_id,
    )

    return agent
