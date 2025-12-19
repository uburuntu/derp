"""Model registry with auto-computed credit pricing.

All models are registered here with their actual API costs. Credit costs
are computed automatically at import time based on the pricing and margin.

To add a new model:
1. Add a ModelConfig to _MODELS list
2. Set is_default=True if it should be the default for its type+tier
3. Credit cost is computed automatically from pricing

To deprecate a model:
1. Set is_deprecated=True
2. Optionally set deprecation_date for warnings
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

# Credit base: 1 credit = $0.001 USD
CREDIT_BASE_USD = Decimal("0.001")
DEFAULT_MARGIN = Decimal("0.80")  # 80% profit margin


class ModelType(StrEnum):
    """Types of models for different capabilities."""

    TEXT = "text"  # Text generation/chat
    IMAGE = "image"  # Image generation
    VOICE = "voice"  # Voice/speech synthesis
    VIDEO = "video"  # Video generation


class ModelTier(StrEnum):
    """Quality tiers for model selection.

    Tiers map to specific models but allow changing the underlying
    model without touching business logic or pricing.
    """

    CHEAP = "cheap"  # Free tier orchestration
    STANDARD = "standard"  # Paid tier default
    PREMIUM = "premium"  # Best quality (/think)


@dataclass
class ModelConfig:
    """Configuration for a registered model with pricing.

    Pricing is specified in USD and credit_cost is computed automatically
    at registration time based on the margin and base rate.
    """

    id: str  # Model identifier, e.g., "gemini-2.0-flash-lite"
    provider: str  # Provider name: "google", "openai", "anthropic", "openrouter"
    display_name: str  # Human-readable name for UI
    model_type: ModelType
    tier: ModelTier

    # Pricing (USD) - source of truth for credit calculation
    # Note: Can be int/float, will be converted to Decimal in calculation
    input_cost_per_1m: Decimal | int | float = Decimal("0")  # Per 1M input tokens
    output_cost_per_1m: Decimal | int | float = Decimal("0")  # Per 1M output tokens
    per_request_cost: Decimal | int | float = Decimal("0")  # Fixed cost per request

    # Capabilities
    max_context_tokens: int = 128000
    supports_tools: bool = True
    supports_vision: bool = True

    # Lifecycle
    is_default: bool = False  # Default for this type+tier combo
    is_deprecated: bool = False
    deprecation_date: date | None = None

    # Computed at registration (NOT manually set)
    _credit_cost: int = field(init=False, default=1)

    @property
    def credit_cost(self) -> int:
        """Credit cost for using this model (computed from pricing)."""
        return self._credit_cost

    def __post_init__(self) -> None:
        """Compute credit cost after initialization."""
        self._credit_cost = calculate_credit_cost(self)


def calculate_credit_cost(
    model: ModelConfig,
    margin: Decimal = DEFAULT_MARGIN,
    avg_tokens: int = 2000,
) -> int:
    """Calculate credits from actual API cost.

    Args:
        model: The model configuration with pricing.
        margin: Profit margin (0.80 = 80% profit).
        avg_tokens: Estimated average tokens per request for cost calculation.

    Returns:
        Credit cost as an integer (minimum 1).
    """
    # Convert to Decimal for consistent arithmetic
    input_cost = Decimal(str(model.input_cost_per_1m))
    output_cost = Decimal(str(model.output_cost_per_1m))
    request_cost = Decimal(str(model.per_request_cost))

    # Calculate token-based cost
    token_cost = (input_cost + output_cost) * avg_tokens / 1_000_000
    total_cost = token_cost + request_cost

    # If zero cost (e.g., missing pricing), minimum 1 credit
    if total_cost == 0:
        return 1

    # Apply margin: cost / (1 - margin) then quantize to credits
    credit_amount = int(total_cost / CREDIT_BASE_USD / (1 - margin))
    return max(1, credit_amount)


# Registry populated at import time
MODEL_REGISTRY: dict[str, ModelConfig] = {}

# Defaults cache for fast lookup
_DEFAULTS: dict[tuple[ModelType, ModelTier], ModelConfig] = {}


def _register_models() -> None:
    """Register all models and compute credit costs."""
    models = [
        # Text models - Cheap tier (free orchestration)
        ModelConfig(
            # Free-tier orchestration model
            # Model details: https://ai.google.dev/gemini-api/docs/models.md.txt
            id="gemini-2.5-flash-lite",
            provider="google",
            display_name="Gemini Flash Lite",
            model_type=ModelType.TEXT,
            tier=ModelTier.CHEAP,
            # TODO: Replace with exact pricing from https://ai.google.dev/gemini-api/docs/pricing.md.txt
            input_cost_per_1m=Decimal("0.01"),
            output_cost_per_1m=Decimal("0.02"),
            is_default=True,
        ),
        # Text models - Standard tier (paid orchestration)
        ModelConfig(
            id="gemini-2.5-flash",
            provider="google",
            display_name="Gemini Flash",
            model_type=ModelType.TEXT,
            tier=ModelTier.STANDARD,
            # TODO: Replace with exact pricing from https://ai.google.dev/gemini-api/docs/pricing.md.txt
            input_cost_per_1m=Decimal("0.15"),
            output_cost_per_1m=Decimal("0.60"),
            is_default=True,
        ),
        # Text models - Premium tier (/think)
        # Gemini 3 Pro: https://ai.google.dev/gemini-api/docs/models.md.txt
        # Pricing: https://ai.google.dev/gemini-api/docs/pricing.md.txt
        ModelConfig(
            id="gemini-3-pro-preview",
            provider="google",
            display_name="Gemini 3 Pro",
            model_type=ModelType.TEXT,
            tier=ModelTier.PREMIUM,
            input_cost_per_1m=Decimal("2.00"),
            output_cost_per_1m=Decimal("12.00"),
            is_default=True,
        ),
        # Legacy premium model (kept for reference)
        ModelConfig(
            id="gemini-2.5-pro",
            provider="google",
            display_name="Gemini 2.5 Pro",
            model_type=ModelType.TEXT,
            tier=ModelTier.PREMIUM,
            input_cost_per_1m=Decimal("1.25"),
            output_cost_per_1m=Decimal("10.00"),
        ),
        # Image generation models
        # Nano Banana: https://ai.google.dev/gemini-api/docs/nanobanana
        ModelConfig(
            id="gemini-2.5-flash-image",
            provider="google",
            display_name="Gemini Flash Image (Nano Banana)",
            model_type=ModelType.IMAGE,
            tier=ModelTier.STANDARD,
            # Pricing note:
            # Gemini native image output is token-priced; docs note $30 / 1M image output tokens
            # with a flat 1290 output tokens for up to 1024x1024 images.
            # Source: https://ai.google.dev/gemini-api/docs/image-generation
            # Cost per 1024px image ≈ 30 * 1290 / 1_000_000 = 0.0387 USD
            per_request_cost=Decimal("0.0387"),
            supports_tools=False,
            is_default=True,
        ),
        # Nano Banana Pro (premium image gen with thinking)
        ModelConfig(
            id="gemini-3-pro-image-preview",
            provider="google",
            display_name="Gemini 3 Pro Image (Nano Banana Pro)",
            model_type=ModelType.IMAGE,
            tier=ModelTier.PREMIUM,
            # Pricing: https://ai.google.dev/gemini-api/docs/pricing.md.txt
            # Output images billed ~ $0.134 per 1K/2K image (see pricing doc)
            per_request_cost=Decimal("0.134"),
            supports_tools=False,
        ),
        # Video generation models (Veo 3.1)
        # Model details: https://ai.google.dev/gemini-api/docs/video.md.txt
        # Pricing: https://ai.google.dev/gemini-api/docs/pricing.md.txt
        ModelConfig(
            id="veo-3.1-fast-generate-preview",
            provider="google",
            display_name="Veo 3.1 Fast",
            model_type=ModelType.VIDEO,
            tier=ModelTier.STANDARD,
            # $0.50 per generated second; default 6s => $3.00
            per_request_cost=Decimal("3.00"),
            supports_tools=False,
            supports_vision=True,
            is_default=True,
        ),
        ModelConfig(
            id="veo-3.1-generate-preview",
            provider="google",
            display_name="Veo 3.1 Standard",
            model_type=ModelType.VIDEO,
            tier=ModelTier.PREMIUM,
            # $1.00 per generated second; default 6s => $6.00
            per_request_cost=Decimal("6.00"),
            supports_tools=False,
            supports_vision=True,
        ),
        # Voice / TTS
        # Model details: https://ai.google.dev/gemini-api/docs/models.md.txt
        # Pricing: https://ai.google.dev/gemini-api/docs/pricing.md.txt
        ModelConfig(
            id="gemini-2.5-pro-preview-tts",
            provider="google",
            display_name="Gemini 2.5 Pro Preview TTS",
            model_type=ModelType.VOICE,
            tier=ModelTier.STANDARD,
            # TODO: Replace with exact pricing from pricing.md.txt (audio token pricing differs)
            input_cost_per_1m=Decimal("0.15"),
            output_cost_per_1m=Decimal("6.00"),
            supports_tools=False,
            supports_vision=False,
            is_default=True,
        ),
        ModelConfig(
            id="dall-e-3",
            provider="openai",
            display_name="DALL-E 3",
            model_type=ModelType.IMAGE,
            tier=ModelTier.PREMIUM,
            per_request_cost=Decimal("0.08"),
            supports_tools=False,
        ),
        # Future: Voice models
        # ModelConfig(
        #     id="eleven-labs-v2",
        #     provider="elevenlabs",
        #     display_name="ElevenLabs V2",
        #     model_type=ModelType.VOICE,
        #     tier=ModelTier.STANDARD,
        #     per_request_cost=Decimal("0.01"),
        #     supports_tools=False,
        #     is_default=True,
        # ),
    ]

    for model in models:
        MODEL_REGISTRY[model.id] = model

        # Track defaults
        if model.is_default:
            key = (model.model_type, model.tier)
            if key in _DEFAULTS:
                msg = f"Duplicate default for {key}: {_DEFAULTS[key].id} and {model.id}"
                raise ValueError(msg)
            _DEFAULTS[key] = model


def get_model(model_id: str) -> ModelConfig:
    """Get a model by its ID.

    Args:
        model_id: The model identifier.

    Returns:
        The model configuration.

    Raises:
        KeyError: If the model is not found.
    """
    return MODEL_REGISTRY[model_id]


def get_default_model(
    model_type: ModelType,
    tier: ModelTier = ModelTier.STANDARD,
) -> ModelConfig:
    """Get the default model for a type and tier.

    Args:
        model_type: The type of model needed.
        tier: The quality tier.

    Returns:
        The default model configuration.

    Raises:
        KeyError: If no default is registered for the type+tier.
    """
    key = (model_type, tier)
    if key not in _DEFAULTS:
        msg = f"No default model for {model_type}/{tier}"
        raise KeyError(msg)
    return _DEFAULTS[key]


# Initialize registry at import
_register_models()
