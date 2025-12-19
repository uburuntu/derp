"""LLM integration module using Pydantic-AI.

This module provides provider-agnostic LLM access with:
- Model tier abstraction (CHEAP/STANDARD/PREMIUM/IMAGE)
- Unified agent factory and dependencies
- Result wrapper for Telegram replies
"""

from derp.llm.agents import create_chat_agent, create_image_agent, create_inline_agent
from derp.llm.deps import AgentDeps
from derp.llm.providers import ModelTier, create_model
from derp.llm.result import AgentResult

__all__ = [
    # Providers
    "ModelTier",
    "create_model",
    # Dependencies
    "AgentDeps",
    # Agents
    "create_chat_agent",
    "create_image_agent",
    "create_inline_agent",
    # Result
    "AgentResult",
]
