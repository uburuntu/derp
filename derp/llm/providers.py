"""Provider factory with model tier abstraction.

Model tiers abstract away specific model names, allowing:
- Easy model upgrades without code changes
- Different quality levels for free vs paid users
- Provider switching via configuration
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from derp.config import settings

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class ModelTier(StrEnum):
    """Quality tiers for model selection.

    Tiers map to specific models but allow changing the underlying
    model without touching business logic or pricing.
    """

    CHEAP = "cheap"  # Free tier, high volume, lowest cost
    STANDARD = "standard"  # Paid default, best quality/cost ratio
    PREMIUM = "premium"  # Best quality, cost secondary
    IMAGE = "image"  # Image generation models


# Tier-to-model mapping. Update when models change or new ones launch.
# Format: "provider:model" or just "model" for default provider
TIER_MODELS: dict[ModelTier, str] = {
    ModelTier.CHEAP: "gemini-2.0-flash-lite",
    ModelTier.STANDARD: "gemini-2.5-flash",
    ModelTier.PREMIUM: "gemini-2.5-pro",
    ModelTier.IMAGE: "gemini-2.5-flash-preview-05-20",
}


def _get_google_api_key() -> str:
    """Get the next Google API key from the rotating iterator."""
    return next(settings.google_api_key_iter)


def create_model(tier: ModelTier = ModelTier.STANDARD) -> Model:
    """Create a Pydantic-AI model for the given quality tier.

    Args:
        tier: The quality tier to use for model selection.

    Returns:
        A configured Pydantic-AI model instance.

    Examples:
        >>> model = create_model(ModelTier.STANDARD)
        >>> model = create_model(ModelTier.CHEAP)  # For free tier
    """
    model_name = TIER_MODELS[tier]

    # For now, we use Google as the primary provider
    # Future: Add FallbackModel with OpenRouter/OpenAI as backup
    provider = GoogleProvider(api_key=_get_google_api_key())

    return GoogleModel(model_name, provider=provider)


def create_image_model() -> Model:
    """Create a model specifically for image generation.

    Uses the IMAGE tier which maps to image-capable models.
    Note: Image generation uses the paid API key for higher limits.
    """
    model_name = TIER_MODELS[ModelTier.IMAGE]

    # Image generation uses the paid key for higher limits
    provider = GoogleProvider(api_key=settings.google_api_paid_key)

    return GoogleModel(model_name, provider=provider)
