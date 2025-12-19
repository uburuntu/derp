"""Tool registry with credit costs and daily limits.

Each tool has:
- Base credit cost (on top of model cost)
- Free daily limit (0 = paid only)
- Model type requirement
- Premium flag (agent sees but can't use without credits)
"""

from __future__ import annotations

from dataclasses import dataclass

from derp.credits.models import ModelType


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """Configuration for a registered tool with credit requirements.

    Tools can have:
    - A free daily limit (free tier users get N uses per day)
    - A base credit cost (added to model cost for paid uses)
    - A default model (or None to use type default)
    """

    name: str
    description: str
    model_type: ModelType  # What type of model it needs
    default_model_id: str | None  # Specific model or None for type default
    base_credit_cost: int  # Base cost (model cost added on top)
    free_daily_limit: int  # 0 = paid only
    is_premium: bool = False  # Agent sees but gets placeholder if no credits

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
        model_type=ModelType.TEXT,
        default_model_id=None,  # Uses orchestrator's model
        base_credit_cost=0,  # Free, uses DuckDuckGo
        free_daily_limit=10,  # Generous free limit
    ),
    # Image generation (premium) - Nano Banana
    # https://ai.google.dev/gemini-api/docs/nanobanana
    "image_generate": ToolConfig(
        name="image_generate",
        description="Generate an image from a text prompt",
        model_type=ModelType.IMAGE,
        default_model_id="gemini-2.5-flash-image",  # Nano Banana
        base_credit_cost=5,  # Base cost on top of model
        free_daily_limit=1,  # One free per day
        is_premium=True,
    ),
    # Image editing (premium) - Nano Banana
    "image_edit": ToolConfig(
        name="image_edit",
        description="Edit an existing image based on instructions",
        model_type=ModelType.IMAGE,
        default_model_id="gemini-2.5-flash-image",  # Nano Banana
        base_credit_cost=5,  # Same as generation
        free_daily_limit=1,  # One free per day
        is_premium=True,
    ),
    # Deep thinking (premium) - Gemini 3 Pro
    # https://ai.google.dev/gemini-api/docs/models#gemini-3
    "think_deep": ToolConfig(
        name="think_deep",
        description="Use advanced reasoning for complex math and logic problems",
        model_type=ModelType.TEXT,
        default_model_id="gemini-3-pro-preview",  # Gemini 3 Pro with Thinking
        base_credit_cost=10,  # Premium reasoning is expensive
        free_daily_limit=0,  # Paid only
        is_premium=True,
    ),
    # Future: Voice generation
    "voice_generate": ToolConfig(
        name="voice_generate",
        description="Generate speech audio from text",
        model_type=ModelType.VOICE,
        default_model_id=None,
        base_credit_cost=3,
        free_daily_limit=0,
        is_premium=True,
    ),
    # Future: Video generation
    "video_generate": ToolConfig(
        name="video_generate",
        description="Generate a short video from a prompt",
        model_type=ModelType.VIDEO,
        default_model_id=None,
        base_credit_cost=20,  # Very expensive
        free_daily_limit=0,
        is_premium=True,
    ),
    # Chat memory (free tool, no model needed)
    "update_memory": ToolConfig(
        name="update_memory",
        description="Update the persistent memory for this chat",
        model_type=ModelType.TEXT,
        default_model_id=None,
        base_credit_cost=0,  # Free
        free_daily_limit=100,  # Effectively unlimited
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
