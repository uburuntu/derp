"""Pydantic AI adapters for catalog-resolved Google models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

from derp.catalog import GoogleModelKey, GoogleModelSpec, get_google_model
from derp.config import settings

if TYPE_CHECKING:
    from pydantic_ai.models import Model


def create_model(
    model: GoogleModelSpec | GoogleModelKey = GoogleModelKey.CHAT_STANDARD,
) -> Model:
    """Create a Pydantic AI model from the catalog's exact specification.

    Args:
        model: Resolved model specification or semantic catalog key.

    Returns:
        A configured Pydantic-AI model instance.

    """
    spec = get_google_model(model) if isinstance(model, GoogleModelKey) else model

    # Paid usage stays outside free-tier data terms and rate limits.
    provider = GoogleProvider(api_key=settings.google_api_paid_key.get_secret_value())

    return GoogleModel(spec.provider_model_id, provider=provider)


def create_image_model(
    model: GoogleModelSpec | GoogleModelKey = GoogleModelKey.IMAGE,
) -> Model:
    """Create the catalog-selected native image model."""
    return create_model(model)


# Safety filtering is disabled here; provider-level policy may still reject content.
# https://ai.google.dev/gemini-api/docs/safety-settings
RELAXED_SAFETY_SETTINGS = GoogleModelSettings(
    google_safety_settings=[
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]
)
