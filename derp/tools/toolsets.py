"""Toolset factory for Pydantic-AI agents.

Provides FunctionToolset instances that can be attached to agents.
Tools are organized by functionality and can be composed as needed.
"""

from __future__ import annotations

import logfire
from pydantic_ai import FunctionToolset
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool

from derp.llm.deps import AgentDeps
from derp.tools.chat_memory import update_chat_memory


def create_chat_toolset() -> FunctionToolset[AgentDeps]:
    """Create the toolset for the chat agent.

    Includes:
    - Chat memory management (update_chat_memory)
    - DuckDuckGo web search (free, no API key required)

    Returns:
        A FunctionToolset configured for chat agent use.
    """
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()

    # Register the chat memory tool
    toolset.tool(update_chat_memory)

    logfire.debug("chat_toolset_created", tools=["update_chat_memory", "duckduckgo"])

    return toolset


def get_search_tool():
    """Get the DuckDuckGo search tool.

    This is a free web search tool that doesn't require an API key.
    It's returned separately so it can be added to agents directly.
    """
    return duckduckgo_search_tool()


def create_premium_toolset() -> FunctionToolset[AgentDeps]:
    """Create the toolset for premium features.

    Includes all chat tools. This is kept separate for future
    tier-based tool access when implementing the credit system.

    Returns:
        A FunctionToolset with premium features.
    """
    return create_chat_toolset()
