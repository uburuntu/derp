"""Tool registry with credit costs and daily limits.

Each tool has one catalog model selection, optional argument-driven variants,
and access limits. Concrete provider IDs never live here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from derp.catalog import (
    AudioPricing,
    GoogleModelKey,
    VideoPricing,
    calculate_credit_cost,
)
from derp.execution import ExecutionPlan, Feature, plan_execution

TTS_MAX_OUTPUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """Configuration for a registered tool with credit requirements.

    Tools can have:
    - A free daily limit (free tier users get N uses per day)
    - A base credit cost (added to model cost for paid uses)
    - An optional catalog model for provider-backed execution
    - Optional argument-driven model variants
    """

    name: str
    description: str
    model_key: GoogleModelKey | None
    feature: Feature | None
    base_credit_cost: int  # Base cost (model cost added on top)
    free_daily_limit: int  # 0 = paid only
    is_premium: bool = False  # Agent sees but gets placeholder if no credits
    audio_output_seconds: int | None = None
    model_parameter: str | None = None
    model_choices: tuple[tuple[str, GoogleModelKey], ...] = ()

    def __post_init__(self) -> None:
        if self.base_credit_cost < 0 or self.free_daily_limit < 0:
            raise ValueError("Tool costs and limits cannot be negative")
        if bool(self.model_parameter) != bool(self.model_choices):
            raise ValueError("Model parameter and choices must be configured together")
        if len(dict(self.model_choices)) != len(self.model_choices):
            raise ValueError("Tool model choices must be unique")
        if self.model_key is None:
            if (
                self.model_choices
                or self.feature is not None
                or self.audio_output_seconds is not None
            ):
                raise ValueError("Provider-free tools cannot define model requirements")
            return
        if self.feature is None:
            raise ValueError("Provider-backed tools must define an execution feature")
        for key in {self.model_key, *(key for _, key in self.model_choices)}:
            plan = plan_execution(self.feature, key)
            if isinstance(plan.model.pricing, AudioPricing) != (
                self.audio_output_seconds is not None
            ):
                raise ValueError(
                    "Audio-backed tools require an explicit output-duration policy"
                )
        if self.audio_output_seconds is not None and self.audio_output_seconds <= 0:
            raise ValueError("Audio output duration must be positive")

    def _resolve_model_key(
        self, arguments: Mapping[str, object]
    ) -> GoogleModelKey | None:
        """Resolve the model without embedding provider identifiers."""
        if self.model_parameter is None:
            return self.model_key
        choice = arguments.get(self.model_parameter)
        return dict(self.model_choices).get(str(choice).lower(), self.model_key)

    def resolve_plan(self, arguments: Mapping[str, object]) -> ExecutionPlan | None:
        """Resolve and validate the exact execution plan for this tool call."""
        model_key = self._resolve_model_key(arguments)
        if model_key is None:
            return None
        if self.feature is None:
            raise ValueError(f"{self.name} resolved a model without a feature")
        return plan_execution(self.feature, model_key)

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
        model = plan.model

        duration_seconds: int | None = None
        audio_input_tokens: int | None = None
        audio_output_seconds: int | None = None
        if isinstance(model.pricing, AudioPricing):
            text = arguments.get("text")
            if not isinstance(text, str):
                raise ValueError("Audio text is required for pricing")
            audio_input_tokens = len(text.encode("utf-8"))
            audio_output_seconds = self.audio_output_seconds
        if isinstance(model.pricing, VideoPricing):
            duration = arguments.get("duration_seconds")
            if duration is not None:
                if isinstance(duration, bool) or not isinstance(duration, int):
                    raise ValueError("Video duration must be an integer")
                duration_seconds = duration
        return calculate_credit_cost(
            model,
            audio_input_tokens=audio_input_tokens,
            audio_output_seconds=audio_output_seconds,
            video_duration_seconds=duration_seconds,
        )

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
    # Deep thinking (premium)
    # https://ai.google.dev/gemini-api/docs/models#gemini-3
    "think_deep": ToolConfig(
        name="think_deep",
        description="Use advanced reasoning for complex math and logic problems",
        model_key=GoogleModelKey.CHAT_REASONING,
        feature=Feature.DEEP_THINK,
        base_credit_cost=10,  # Premium reasoning is expensive
        free_daily_limit=0,  # Paid only
        is_premium=True,
    ),
    # Voice / TTS
    "voice_tts": ToolConfig(
        name="voice_tts",
        description="Generate speech audio from text",
        model_key=GoogleModelKey.TTS,
        feature=Feature.TTS,
        base_credit_cost=3,
        free_daily_limit=0,
        is_premium=True,
        audio_output_seconds=TTS_MAX_OUTPUT_SECONDS,
    ),
    # Video generation (Veo 3.1)
    "video_generate": ToolConfig(
        name="video_generate",
        description="Generate a short video from a prompt",
        model_key=GoogleModelKey.VIDEO_FAST,
        feature=Feature.VIDEO_GENERATE,
        base_credit_cost=20,  # Very expensive
        free_daily_limit=0,
        is_premium=True,
        model_parameter="quality",
        model_choices=(
            ("fast", GoogleModelKey.VIDEO_FAST),
            ("standard", GoogleModelKey.VIDEO_STANDARD),
        ),
    ),
    # Chat memory (free tool, no model needed)
    "update_memory": ToolConfig(
        name="update_memory",
        description="Update the persistent memory for this chat",
        model_key=None,
        feature=None,
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
