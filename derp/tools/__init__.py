"""Tools for Pydantic-AI agents.

Concrete tools are exposed lazily so importing tool policy does not initialize
the agent and provider graph.
"""

from typing import Any

__all__ = [
    "web_search",
    "generate_image",
    "create_chat_toolset",
    "credit_aware_tool",
]


def __getattr__(name: str) -> Any:
    """Resolve compatibility exports without creating import cycles."""
    match name:
        case "web_search":
            from derp.tools.web_search import web_search

            return web_search
        case "generate_image":
            from derp.tools.gemini_image import generate_image

            return generate_image
        case "create_chat_toolset":
            from derp.tools.toolsets import create_chat_toolset

            return create_chat_toolset
        case "credit_aware_tool":
            from derp.tools.wrapper import credit_aware_tool

            return credit_aware_tool
        case _:
            raise AttributeError(name)
