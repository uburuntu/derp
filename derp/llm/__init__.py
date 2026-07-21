"""LLM integration module using Pydantic-AI.

This module provides LLM access with:
- Catalog-resolved provider models
- Unified agent factory and dependencies
- Result wrapper for Telegram replies
"""

from derp.catalog import ModelRole, ModelSpec, get_openrouter_model
from derp.llm.agents import create_chat_agent, create_image_agent, create_inline_agent
from derp.llm.deps import AgentDeps
from derp.llm.inline_executor import PydanticAIInlineExecutor
from derp.llm.providers import (
    RELAXED_SAFETY_SETTINGS,
    create_model,
    model_run_settings,
    pseudonymous_inference_user,
)
from derp.llm.result import (
    AgentContentDelivered,
    AgentContentUnavailable,
    AgentDeliveryOutcome,
    AgentResult,
)

__all__ = [
    # Providers
    "ModelRole",
    "ModelSpec",
    "get_openrouter_model",
    "create_model",
    "RELAXED_SAFETY_SETTINGS",
    "model_run_settings",
    "pseudonymous_inference_user",
    # Dependencies
    "AgentDeps",
    # Agents
    "create_chat_agent",
    "create_image_agent",
    "create_inline_agent",
    "PydanticAIInlineExecutor",
    # Result
    "AgentContentDelivered",
    "AgentContentUnavailable",
    "AgentDeliveryOutcome",
    "AgentResult",
]
