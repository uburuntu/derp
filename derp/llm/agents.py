"""Agent factory for creating Pydantic-AI agents.

Provides factory functions for different agent types:
- Chat agent: Full-featured with tools and context
- Image agent: For image generation/editing
- Inline agent: Lightweight for inline queries
"""

from __future__ import annotations

import logfire
from pydantic_ai import Agent, BinaryImage
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool

from derp.llm.deps import AgentDeps
from derp.llm.prompts import (
    BASE_SYSTEM_PROMPT,
    IMAGE_SYSTEM_PROMPT,
    INLINE_SYSTEM_PROMPT,
    build_chat_system_prompt,
)
from derp.llm.providers import ModelTier, create_image_model, create_model

# Module-level agent instances (lazy initialization)
_chat_agent: Agent[AgentDeps, str] | None = None
_image_agent: Agent[None, BinaryImage | str] | None = None
_inline_agent: Agent[None, str] | None = None


def create_chat_agent(tier: ModelTier = ModelTier.STANDARD) -> Agent[AgentDeps, str]:
    """Create the main chat agent with tools and context.

    The chat agent is the primary agent for handling messages in chats.
    It has access to tools (memory, DuckDuckGo search) and full conversation context.

    Args:
        tier: The model tier to use (affects quality and cost).

    Returns:
        A configured Agent instance for chat interactions.
    """
    model = create_model(tier)

    # Include DuckDuckGo search tool by default (free, no API key)
    search_tool = duckduckgo_search_tool()

    agent: Agent[AgentDeps, str] = Agent(
        model,
        deps_type=AgentDeps,
        output_type=str,
        instructions=BASE_SYSTEM_PROMPT,
        tools=[search_tool],
    )

    # Add dynamic system prompt for chat memory
    @agent.system_prompt
    def add_chat_context(ctx) -> str:
        return build_chat_system_prompt(ctx)

    logfire.debug("chat_agent_created", tier=tier.value, has_search=True)

    return agent


def create_image_agent() -> Agent[None, BinaryImage | str]:
    """Create an agent for image generation and editing.

    Uses the IMAGE tier model which supports native image generation.
    Returns either a BinaryImage or text (if image generation fails/is refused).

    Returns:
        A configured Agent instance for image generation.
    """
    model = create_image_model()

    agent: Agent[None, BinaryImage | str] = Agent(
        model,
        output_type=BinaryImage | str,
        instructions=IMAGE_SYSTEM_PROMPT,
    )

    logfire.debug("image_agent_created")

    return agent


def create_inline_agent(tier: ModelTier = ModelTier.CHEAP) -> Agent[None, str]:
    """Create a lightweight agent for inline queries.

    Uses the CHEAP tier by default for cost efficiency on high-volume
    inline queries. No tools or complex context.

    Args:
        tier: The model tier to use (defaults to CHEAP for cost efficiency).

    Returns:
        A configured Agent instance for inline queries.
    """
    model = create_model(tier)

    agent: Agent[None, str] = Agent(
        model,
        output_type=str,
        instructions=INLINE_SYSTEM_PROMPT,
    )

    logfire.debug("inline_agent_created", tier=tier.value)

    return agent


def get_chat_agent(tier: ModelTier = ModelTier.STANDARD) -> Agent[AgentDeps, str]:
    """Get or create a cached chat agent instance.

    For most use cases, use this instead of create_chat_agent()
    to reuse the same agent instance.

    Args:
        tier: The model tier to use.

    Returns:
        A cached or newly created chat agent.
    """
    global _chat_agent
    if _chat_agent is None:
        _chat_agent = create_chat_agent(tier)
    return _chat_agent


def get_image_agent() -> Agent[None, BinaryImage | str]:
    """Get or create a cached image agent instance.

    Returns:
        A cached or newly created image agent.
    """
    global _image_agent
    if _image_agent is None:
        _image_agent = create_image_agent()
    return _image_agent


def get_inline_agent(tier: ModelTier = ModelTier.CHEAP) -> Agent[None, str]:
    """Get or create a cached inline agent instance.

    Args:
        tier: The model tier to use.

    Returns:
        A cached or newly created inline agent.
    """
    global _inline_agent
    if _inline_agent is None:
        _inline_agent = create_inline_agent(tier)
    return _inline_agent
