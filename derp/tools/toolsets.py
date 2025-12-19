"""Toolset factory for Pydantic-AI agents.

Provides FunctionToolset instances that can be attached to agents.
Tools are organized by functionality and composed based on needs.

All tools that cost credits are wrapped with @credit_aware_tool
which handles access checking and deduction automatically.
"""

from __future__ import annotations

import logfire
from pydantic_ai import FunctionToolset

from derp.llm.deps import AgentDeps
from derp.tools.chat_memory import update_chat_memory
from derp.tools.image_gen import edit_image, generate_image
from derp.tools.think import think_deep
from derp.tools.web_search import web_search


def create_chat_toolset() -> FunctionToolset[AgentDeps]:
    """Create the full toolset for the chat agent.

    Includes all tools - both free and premium. The credit_aware_tool
    decorator handles access control, so the agent sees all tools but
    will get placeholder responses for premium tools if no credits.

    Tools included:
    - update_chat_memory: Save persistent memory (free, high limit)
    - web_search: DuckDuckGo search (free, daily limit)
    - generate_image: Image generation (premium, 1 free/day)
    - think_deep: Premium reasoning (premium, no free)

    Returns:
        A FunctionToolset configured for chat agent use.
    """
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()

    # Free tools (no credit cost, but may have daily limits)
    toolset.tool(update_chat_memory)
    toolset.tool(web_search)

    # Premium tools (require credits or use daily free limit)
    toolset.tool(generate_image)
    toolset.tool(edit_image)
    toolset.tool(think_deep)

    logfire.debug(
        "chat_toolset_created",
        tools=[
            "update_chat_memory",
            "web_search",
            "generate_image",
            "edit_image",
            "think_deep",
        ],
    )

    return toolset


def create_free_toolset() -> FunctionToolset[AgentDeps]:
    """Create a minimal toolset for free tier.

    Only includes tools that are completely free or have generous
    free limits. Premium tools are excluded so the agent doesn't
    see them at all (alternative to placeholder approach).

    Returns:
        A FunctionToolset with only free tools.
    """
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()

    toolset.tool(update_chat_memory)
    toolset.tool(web_search)

    logfire.debug(
        "free_toolset_created",
        tools=["update_chat_memory", "web_search"],
    )

    return toolset
