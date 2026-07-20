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
    GoogleModelSpec,
    ModelCapability,
    VideoPricing,
    calculate_credit_cost,
    get_google_model,
)

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
    base_credit_cost: int  # Base cost (model cost added on top)
    free_daily_limit: int  # 0 = paid only
    is_premium: bool = False  # Agent sees but gets placeholder if no credits
    required_capabilities: frozenset[ModelCapability] = frozenset()
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
                or self.required_capabilities
                or self.audio_output_seconds is not None
            ):
                raise ValueError("Provider-free tools cannot define model requirements")
            return
        if not self.required_capabilities:
            raise ValueError("Provider-backed tools must define required capabilities")
        for key in {self.model_key, *(key for _, key in self.model_choices)}:
            spec = get_google_model(key)
            missing = self.required_capabilities - spec.capabilities
            if missing:
                raise ValueError(
                    f"{self.name} requires {', '.join(sorted(cap.value for cap in missing))} from {key}"
                )
            if isinstance(spec.pricing, AudioPricing) != (
                self.audio_output_seconds is not None
            ):
                raise ValueError(
                    "Audio-backed tools require an explicit output-duration policy"
                )
        if self.audio_output_seconds is not None and self.audio_output_seconds <= 0:
            raise ValueError("Audio output duration must be positive")

    def resolve_model_key(
        self, arguments: Mapping[str, object]
    ) -> GoogleModelKey | None:
        """Resolve the model without embedding provider identifiers."""
        if self.model_parameter is None:
            return self.model_key
        choice = arguments.get(self.model_parameter)
        return dict(self.model_choices).get(str(choice).lower(), self.model_key)

    def model_credit_cost(
        self,
        model: GoogleModelSpec | None,
        arguments: Mapping[str, object],
    ) -> int:
        """Price provider work from the same model and controllable usage."""
        if model is None:
            return 0
        if self.model_key is None:
            raise ValueError(f"{self.name} cannot carry a provider model")

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
        base_credit_cost=0,  # Free, uses DuckDuckGo
        free_daily_limit=10,  # Generous free limit
    ),
    # Image generation (premium)
    # https://ai.google.dev/gemini-api/docs/nanobanana
    "image_generate": ToolConfig(
        name="image_generate",
        description="Generate an image from a text prompt",
        model_key=GoogleModelKey.IMAGE,
        base_credit_cost=5,  # Base cost on top of model
        free_daily_limit=1,  # One free per day
        is_premium=True,
        required_capabilities=frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.IMAGE_OUTPUT}
        ),
    ),
    # Image editing (premium)
    "image_edit": ToolConfig(
        name="image_edit",
        description="Edit an existing image based on instructions",
        model_key=GoogleModelKey.IMAGE,
        base_credit_cost=5,  # Same as generation
        free_daily_limit=1,  # One free per day
        is_premium=True,
        required_capabilities=frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.IMAGE_OUTPUT,
            }
        ),
    ),
    # Deep thinking (premium)
    # https://ai.google.dev/gemini-api/docs/models#gemini-3
    "think_deep": ToolConfig(
        name="think_deep",
        description="Use advanced reasoning for complex math and logic problems",
        model_key=GoogleModelKey.CHAT_REASONING,
        base_credit_cost=10,  # Premium reasoning is expensive
        free_daily_limit=0,  # Paid only
        is_premium=True,
        required_capabilities=frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.TEXT_OUTPUT,
                ModelCapability.THINKING,
            }
        ),
    ),
    # Voice / TTS
    "voice_tts": ToolConfig(
        name="voice_tts",
        description="Generate speech audio from text",
        model_key=GoogleModelKey.TTS,
        base_credit_cost=3,
        free_daily_limit=0,
        is_premium=True,
        required_capabilities=frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.AUDIO_OUTPUT}
        ),
        audio_output_seconds=TTS_MAX_OUTPUT_SECONDS,
    ),
    # Video generation (Veo 3.1)
    "video_generate": ToolConfig(
        name="video_generate",
        description="Generate a short video from a prompt",
        model_key=GoogleModelKey.VIDEO_FAST,
        base_credit_cost=20,  # Very expensive
        free_daily_limit=0,
        is_premium=True,
        required_capabilities=frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.VIDEO_OUTPUT}
        ),
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
