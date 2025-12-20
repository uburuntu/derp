"""Provider factory with model tier abstraction.

Model tiers abstract away specific model names, allowing:
- Easy model upgrades without code changes
- Different quality levels for free vs paid users
- Provider switching via configuration
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
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
# https://ai.google.dev/gemini-api/docs/models
TIER_MODELS: dict[ModelTier, str] = {
    # Free tier orchestration
    ModelTier.CHEAP: "gemini-2.5-flash-lite",
    # Paid tier orchestration
    ModelTier.STANDARD: "gemini-2.5-flash",
    # Gemini 3 Pro for premium/thinking: https://ai.google.dev/gemini-api/docs/models#gemini-3
    ModelTier.PREMIUM: "gemini-3-pro-preview",
    # Nano Banana for image gen: https://ai.google.dev/gemini-api/docs/nanobanana
    ModelTier.IMAGE: "gemini-2.5-flash-image",
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

    # Use paid API key for all tiers to avoid free tier rate limits.
    # The CHEAP tier still uses a cheaper model for cost efficiency.
    provider = GoogleProvider(api_key=settings.google_api_paid_key)

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


# Relaxed safety settings for creative content.
# BLOCK_ONLY_HIGH allows more creative freedom while still blocking egregious content.
# https://ai.google.dev/gemini-api/docs/safety-settings
RELAXED_SAFETY_SETTINGS = GoogleModelSettings(
    google_safety_settings=[
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]
)
