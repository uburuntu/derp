"""Agent factory for creating Pydantic-AI agents.

Provides factory functions for different agent types:
- Chat agent: Full-featured with tools and context
- Image agent: For image generation/editing
- Inline agent: Lightweight for inline queries
"""

from __future__ import annotations

import logfire
from pydantic_ai import Agent, BinaryImage, RunContext

from derp.catalog import (
    GoogleModelKey,
    GoogleModelSpec,
    ModelCapability,
    get_google_model,
)
from derp.llm.deps import AgentDeps
from derp.llm.prompts import (
    IMAGE_SYSTEM_PROMPT,
    INLINE_SYSTEM_PROMPT,
    build_chat_system_prompt,
)
from derp.llm.providers import create_image_model, create_model


def _resolve_capable_model(
    model: GoogleModelSpec | GoogleModelKey,
    *required: ModelCapability,
) -> GoogleModelSpec:
    """Resolve a catalog model and enforce the agent's runtime contract."""
    spec = get_google_model(model) if isinstance(model, GoogleModelKey) else model
    missing = [
        capability.value
        for capability in required
        if capability not in spec.capabilities
    ]
    if missing:
        raise ValueError(
            f"{spec.key.value} lacks required capabilities: {', '.join(missing)}"
        )
    return spec


def create_chat_agent(
    model: GoogleModelSpec | GoogleModelKey = GoogleModelKey.CHAT_STANDARD,
) -> Agent[AgentDeps, str]:
    """Create the main chat agent with tools and context.

    The chat agent is the primary agent for handling messages in chats.
    Tools are attached by the caller so all capabilities share the same policy layer.

    Args:
        model: Exact catalog model or semantic catalog key.

    Returns:
        A configured Agent instance for chat interactions.
    """
    spec = _resolve_capable_model(
        model,
        ModelCapability.TEXT_OUTPUT,
        ModelCapability.TOOLS,
    )
    provider_model = create_model(spec)

    agent: Agent[AgentDeps, str] = Agent(
        provider_model,
        name="chat",
        deps_type=AgentDeps,
        output_type=str,
    )

    @agent.instructions
    def add_chat_context(ctx: RunContext[AgentDeps]) -> str:
        return build_chat_system_prompt(ctx)

    logfire.debug(
        "chat_agent_created",
        model_key=spec.key.value,
        model=spec.provider_model_id,
    )

    return agent


def create_image_agent(
    model: GoogleModelSpec | GoogleModelKey = GoogleModelKey.IMAGE,
) -> Agent[object, BinaryImage | str]:
    """Create an agent for image generation and editing.

    Uses the catalog image model which supports native image generation.
    Returns either a BinaryImage or text (if image generation fails/is refused).

    Returns:
        A configured Agent instance for image generation.
    """
    spec = _resolve_capable_model(model, ModelCapability.IMAGE_OUTPUT)
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
        model_key=spec.key.value,
        model=spec.provider_model_id,
    )

    return agent


def create_inline_agent(
    model: GoogleModelSpec | GoogleModelKey = GoogleModelKey.CHAT_ECONOMY,
) -> Agent[object, str]:
    """Create a lightweight agent for inline queries.

    Uses the economy catalog model by default for cost efficiency on high-volume
    inline queries. No tools or complex context.

    Args:
        model: Exact catalog model or semantic catalog key.

    Returns:
        A configured Agent instance for inline queries.
    """
    spec = _resolve_capable_model(model, ModelCapability.TEXT_OUTPUT)
    provider_model = create_model(spec)

    agent: Agent[object, str] = Agent(
        provider_model,
        name="inline",
        output_type=str,
        instructions=INLINE_SYSTEM_PROMPT,
    )

    logfire.debug(
        "inline_agent_created",
        model_key=spec.key.value,
        model=spec.provider_model_id,
    )

    return agent
