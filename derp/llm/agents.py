"""Agent factory for creating Pydantic-AI agents.

Provides factory functions for different agent types:
- Chat agent: Full-featured with tools and context
- Image agent: For image generation/editing
- Inline agent: Lightweight for inline queries
"""

from __future__ import annotations

import logfire
from pydantic_ai import Agent, BinaryImage, RunContext

from derp.llm.deps import AgentDeps
from derp.llm.prompts import (
    IMAGE_SYSTEM_PROMPT,
    INLINE_SYSTEM_PROMPT,
    build_chat_system_prompt,
)
from derp.llm.providers import ModelTier, create_image_model, create_model


def create_chat_agent(tier: ModelTier = ModelTier.STANDARD) -> Agent[AgentDeps, str]:
    """Create the main chat agent with tools and context.

    The chat agent is the primary agent for handling messages in chats.
    Tools are attached by the caller so all capabilities share the same policy layer.

    Args:
        tier: The model tier to use (affects quality and cost).

    Returns:
        A configured Agent instance for chat interactions.
    """
    model = create_model(tier)

    agent: Agent[AgentDeps, str] = Agent(
        model,
        name="chat",
        deps_type=AgentDeps,
        output_type=str,
    )

    @agent.instructions
    def add_chat_context(ctx: RunContext[AgentDeps]) -> str:
        return build_chat_system_prompt(ctx)

    logfire.debug("chat_agent_created", tier=tier.value)

    return agent


def create_image_agent() -> Agent[object, BinaryImage | str]:
    """Create an agent for image generation and editing.

    Uses the IMAGE tier model which supports native image generation.
    Returns either a BinaryImage or text (if image generation fails/is refused).

    Returns:
        A configured Agent instance for image generation.
    """
    model = create_image_model()

    agent: Agent[object, BinaryImage | str] = Agent(
        model,
        name="image",
        output_type=BinaryImage | str,
        instructions=IMAGE_SYSTEM_PROMPT,
        retries=2,  # Image models may need more attempts for output validation
    )

    logfire.debug("image_agent_created")

    return agent


def create_inline_agent(tier: ModelTier = ModelTier.CHEAP) -> Agent[object, str]:
    """Create a lightweight agent for inline queries.

    Uses the CHEAP tier by default for cost efficiency on high-volume
    inline queries. No tools or complex context.

    Args:
        tier: The model tier to use (defaults to CHEAP for cost efficiency).

    Returns:
        A configured Agent instance for inline queries.
    """
    model = create_model(tier)

    agent: Agent[object, str] = Agent(
        model,
        name="inline",
        output_type=str,
        instructions=INLINE_SYSTEM_PROMPT,
    )

    logfire.debug("inline_agent_created", tier=tier.value)

    return agent
