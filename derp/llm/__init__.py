"""LLM integration module using Pydantic-AI.

This module provides LLM access with:
- Catalog-resolved Google models
- Unified agent factory and dependencies
- Result wrapper for Telegram replies
"""

from derp.catalog import GoogleModelKey, GoogleModelSpec, get_google_model
from derp.llm.agents import create_chat_agent, create_image_agent, create_inline_agent
from derp.llm.deps import AgentDeps
from derp.llm.inline_executor import PydanticAIInlineExecutor
from derp.llm.providers import RELAXED_SAFETY_SETTINGS, create_model
from derp.llm.result import AgentResult

__all__ = [
    # Providers
    "GoogleModelKey",
    "GoogleModelSpec",
    "get_google_model",
    "create_model",
    "RELAXED_SAFETY_SETTINGS",
    # Dependencies
    "AgentDeps",
    # Agents
    "create_chat_agent",
    "create_image_agent",
    "create_inline_agent",
    "PydanticAIInlineExecutor",
    # Result
    "AgentResult",
]
