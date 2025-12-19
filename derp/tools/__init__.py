"""Tools for Pydantic-AI agents.

This module provides tools that can be attached to agents via FunctionToolset.
All premium tools are wrapped with @credit_aware_tool for automatic
access checking and credit deduction.
"""

from derp.tools.chat_memory import update_chat_memory
from derp.tools.image_gen import generate_image
from derp.tools.think import think_deep
from derp.tools.toolsets import create_chat_toolset, create_free_toolset
from derp.tools.web_search import web_search
from derp.tools.wrapper import credit_aware_tool, get_tool_cost, is_premium_tool

__all__ = [
    # Tool functions
    "update_chat_memory",
    "web_search",
    "generate_image",
    "think_deep",
    # Toolset factories
    "create_chat_toolset",
    "create_free_toolset",
    # Wrapper utilities
    "credit_aware_tool",
    "is_premium_tool",
    "get_tool_cost",
]
