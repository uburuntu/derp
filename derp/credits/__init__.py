"""Credit economy module for monetization.

Provides:
- Model registry with auto-computed pricing
- Tool registry with credit costs and daily limits
- Credit service for checking/deducting credits
- Types for credit operations
"""

from derp.credits.models import (
    CREDIT_BASE_USD,
    DEFAULT_MARGIN,
    MODEL_REGISTRY,
    ModelConfig,
    ModelTier,
    ModelType,
    calculate_credit_cost,
    get_default_model,
    get_model,
)
from derp.credits.service import CONTEXT_LIMITS, CreditService, get_placeholder_message
from derp.credits.tools import TOOL_REGISTRY, ToolConfig, get_tool
from derp.credits.types import CreditCheckResult, TransactionType

__all__ = [
    # Model registry
    "ModelType",
    "ModelTier",
    "ModelConfig",
    "MODEL_REGISTRY",
    "get_model",
    "get_default_model",
    "calculate_credit_cost",
    "CREDIT_BASE_USD",
    "DEFAULT_MARGIN",
    # Tool registry
    "ToolConfig",
    "TOOL_REGISTRY",
    "get_tool",
    # Service
    "CreditService",
    "CONTEXT_LIMITS",
    "get_placeholder_message",
    # Types
    "CreditCheckResult",
    "TransactionType",
]
