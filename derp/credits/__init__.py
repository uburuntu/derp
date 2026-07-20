"""Credit economy module for monetization.

Provides:
- Tool registry with credit costs and daily limits
- Credit service for checking/deducting credits
- Types for credit operations
"""

from derp.catalog import (
    CREDIT_BASE_USD,
    DEFAULT_MARGIN,
    GOOGLE_MODEL_CATALOG,
    GoogleModelKey,
    GoogleModelSpec,
    calculate_credit_cost,
    get_google_model,
    get_google_model_by_id,
)
from derp.credits.service import CONTEXT_LIMITS, CreditService, get_placeholder_message
from derp.credits.tools import TOOL_REGISTRY, ToolConfig, get_tool
from derp.credits.types import CreditCheckResult

__all__ = [
    # Google model catalog
    "GoogleModelKey",
    "GoogleModelSpec",
    "GOOGLE_MODEL_CATALOG",
    "get_google_model",
    "get_google_model_by_id",
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
]
