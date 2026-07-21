"""Tool registry with credit costs and daily limits.

Each tool has one catalog model selection, optional argument-driven variants,
and access limits. Concrete provider IDs never live here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from derp.catalog import GoogleModelKey, calculate_credit_cost
from derp.execution import ExecutionPlan, Feature, plan_execution


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """Configuration for a registered tool with credit requirements.

    Tools can have:
    - A free daily limit (free tier users get N uses per day)
    - A base credit cost (added to model cost for paid uses)
    - An optional catalog model for provider-backed execution
    """

    name: str
    description: str
    model_key: GoogleModelKey | None
    feature: Feature | None
    base_credit_cost: int  # Base cost (model cost added on top)
    free_daily_limit: int  # 0 = paid only
    is_premium: bool = False  # Agent sees but gets placeholder if no credits

    def __post_init__(self) -> None:
        if self.base_credit_cost < 0 or self.free_daily_limit < 0:
            raise ValueError("Tool costs and limits cannot be negative")
        if self.model_key is None:
            if self.feature is not None:
                raise ValueError("Provider-free tools cannot define model requirements")
            return
        if self.feature is None:
            raise ValueError("Provider-backed tools must define an execution feature")
        plan_execution(self.feature, self.model_key)

    def resolve_plan(self, arguments: Mapping[str, object]) -> ExecutionPlan | None:
        """Resolve and validate the exact execution plan for this tool call."""
        if self.model_key is None:
            return None
        if self.feature is None:
            raise ValueError(f"{self.name} resolved a model without a feature")
        return plan_execution(self.feature, self.model_key)

    def model_credit_cost(
        self,
        plan: ExecutionPlan | None,
        arguments: Mapping[str, object],
    ) -> int:
        """Price provider work from the same model and controllable usage."""
        if plan is None:
            return 0
        if self.feature is None or plan.feature is not self.feature:
            raise ValueError(f"{self.name} received an incompatible execution plan")
        return calculate_credit_cost(plan.model)

    def total_cost(self, model_credit_cost: int) -> int:
        """Calculate total credit cost including model cost.

        Args:
            model_credit_cost: The credit cost of the model being used.

        Returns:
            Total credits required (base + model).
        """
        return self.base_credit_cost + model_credit_cost


# Tool registry - add new tools here
TOOL_REGISTRY: dict[str, ToolConfig] = {
    # Free tools (DuckDuckGo search)
    "web_search": ToolConfig(
        name="web_search",
        description="Search the web for current information using DuckDuckGo",
        model_key=None,
        feature=None,
        base_credit_cost=0,  # Free, uses DuckDuckGo
        free_daily_limit=10,  # Generous free limit
    ),
    # Image generation (premium)
    # https://ai.google.dev/gemini-api/docs/nanobanana
    "image_generate": ToolConfig(
        name="image_generate",
        description="Generate an image from a text prompt",
        model_key=GoogleModelKey.IMAGE,
        feature=Feature.IMAGE_GENERATE,
        base_credit_cost=5,  # Base cost on top of model
        free_daily_limit=1,  # One free per day
        is_premium=True,
    ),
    # Image editing (premium)
    "image_edit": ToolConfig(
        name="image_edit",
        description="Edit an existing image based on instructions",
        model_key=GoogleModelKey.IMAGE,
        feature=Feature.IMAGE_EDIT,
        base_credit_cost=5,  # Same as generation
        free_daily_limit=1,  # One free per day
        is_premium=True,
    ),
}


def get_tool(tool_name: str) -> ToolConfig:
    """Get a tool configuration by name.

    Args:
        tool_name: The tool identifier.

    Returns:
        The tool configuration.

    Raises:
        KeyError: If the tool is not found.
    """
    return TOOL_REGISTRY[tool_name]
