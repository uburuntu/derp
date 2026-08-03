"""Pydantic AI adapters for provider-neutral catalog models."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import TYPE_CHECKING, cast
from uuid import UUID

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openrouter import (
    OpenRouterModel,
    OpenRouterModelSettings,
    OpenRouterProviderConfig,
    OpenRouterReasoning,
)
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.catalog import (
    DataCollectionPolicy,
    InferenceProvider,
    ModelRole,
    ModelSpec,
    PriceCeiling,
    get_openrouter_model,
)
from derp.config import settings as app_settings

if TYPE_CHECKING:
    from pydantic_ai.models import Model, ModelSettings


_openrouter_provider: OpenRouterProvider | None = None
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_TIMEOUT = httpx.Timeout(120.0, connect=10.0, write=30.0, pool=10.0)
_OPTIONAL_NON_REASONING_ROLES = frozenset(
    {
        ModelRole.CHAT_ECONOMY,
        ModelRole.CHAT_STANDARD,
        ModelRole.FREE_TEXT,
        ModelRole.FREE_VISUAL,
        ModelRole.FREE_AUDIO,
    }
)


def _shared_openrouter_provider() -> OpenRouterProvider:
    """Return the process-owned OpenRouter provider used by all agents."""
    global _openrouter_provider
    if _openrouter_provider is None:
        if app_settings.openrouter_api_key is None:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required for OpenRouter inference"
            )
        http_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            timeout=_OPENROUTER_TIMEOUT,
            follow_redirects=False,
        )
        client = AsyncOpenAI(
            api_key=app_settings.openrouter_api_key.get_secret_value(),
            base_url=_OPENROUTER_BASE_URL,
            timeout=_OPENROUTER_TIMEOUT,
            max_retries=0,
            default_headers={
                "HTTP-Referer": app_settings.resolved_openrouter_app_url,
                "X-OpenRouter-Title": app_settings.openrouter_app_title,
            },
            http_client=http_client,
        )
        _openrouter_provider = OpenRouterProvider(openai_client=client)
    return _openrouter_provider


async def close_model_providers() -> None:
    """Close process-owned provider transports at application shutdown."""
    global _openrouter_provider
    provider, _openrouter_provider = _openrouter_provider, None
    if provider is not None:
        await provider.client.close()


def _ceiling_value(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _max_price(ceiling: PriceCeiling | None) -> dict[str, float]:
    if ceiling is None:
        return {}
    values = {
        "prompt": _ceiling_value(ceiling.prompt),
        "completion": _ceiling_value(ceiling.completion),
        "image": _ceiling_value(ceiling.image),
        "audio": _ceiling_value(ceiling.audio),
        "request": _ceiling_value(ceiling.request),
    }
    return {name: value for name, value in values.items() if value is not None}


def _reasoning_policy(model: ModelSpec) -> OpenRouterReasoning | None:
    """Make chat reasoning intentional instead of accepting provider defaults."""
    if model.key is ModelRole.CHAT_REASONING:
        return {"enabled": True, "effort": "high"}
    if model.key is ModelRole.CHAT_MULTIMODAL:
        # The reviewed Gemini 3.5 Flash catalog marks reasoning as mandatory.
        return {"enabled": True, "effort": "minimal"}
    if model.key in _OPTIONAL_NON_REASONING_ROLES:
        return {"enabled": False}
    return None


def openrouter_settings(
    model: ModelSpec,
    *,
    pseudonymous_user: str | None = None,
) -> OpenRouterModelSettings:
    """Translate reviewed routing policy to one request settings object."""
    if model.provider is not InferenceProvider.OPENROUTER:
        raise ValueError("OpenRouter settings require an OpenRouter catalog model")
    if model.routing is None:
        raise ValueError(f"{model.provider_model_id} has no reviewed routing policy")

    route = model.routing
    provider: OpenRouterProviderConfig = {
        "allow_fallbacks": route.allow_fallbacks,
        "require_parameters": route.require_parameters,
        "data_collection": route.data_collection.value,
        "zdr": route.zero_data_retention,
    }
    if route.provider_order:
        provider["order"] = list(route.provider_order)
    if maximums := _max_price(route.max_price):
        provider["max_price"] = cast("object", maximums)  # type: ignore[assignment]

    result: OpenRouterModelSettings = {
        "openrouter_provider": provider,
        "openrouter_usage": {"include": True},
    }
    if reasoning := _reasoning_policy(model):
        result["openrouter_reasoning"] = reasoning
    if pseudonymous_user:
        result["openai_user"] = pseudonymous_user  # type: ignore[typeddict-unknown-key]
    if (
        route.data_collection is DataCollectionPolicy.DENY
        and model.provider_model_id.startswith("anthropic/")
    ):
        result.update(
            openrouter_cache_instructions="5m",
            openrouter_cache_messages="5m",
            openrouter_cache_tool_definitions="5m",
        )
    return result


def create_model(
    model: ModelSpec | ModelRole = ModelRole.CHAT_STANDARD,
) -> Model:
    """Create the exact provider model selected by the execution plan."""
    spec = get_openrouter_model(model) if isinstance(model, ModelRole) else model
    if not spec.available:
        raise ValueError(
            f"{spec.provider_model_id} is unavailable in the reviewed catalog"
        )

    if spec.provider is InferenceProvider.OPENROUTER:
        return OpenRouterModel(
            spec.provider_model_id,
            provider=_shared_openrouter_provider(),
            settings=openrouter_settings(spec),
        )

    provider = GoogleProvider(
        api_key=app_settings.google_api_paid_key.get_secret_value()
    )
    return GoogleModel(spec.provider_model_id, provider=provider)


def create_image_model(
    model: ModelSpec | ModelRole = ModelRole.IMAGE,
) -> Model:
    """Create the catalog-selected native image model."""
    return create_model(model)


def pseudonymous_inference_user(user_id: UUID) -> str:
    """Create a stable gateway abuse-control ID without exposing Telegram identity."""
    digest = hmac.new(
        app_settings.callback_signing_key,
        b"derp:openrouter-user:v1\0" + user_id.bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"derp_{digest[:32]}"


# Safety filtering is disabled on the explicit direct-Google rollback path only.
GOOGLE_RELAXED_SAFETY_SETTINGS = GoogleModelSettings(
    google_safety_settings=[
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]
)


def model_run_settings(
    model: ModelSpec,
    *,
    user_id: UUID | None = None,
    max_tokens: int | None = None,
) -> ModelSettings:
    """Return provider-correct settings for a single model request."""
    if max_tokens is not None and (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ValueError("max_tokens must be a positive integer")
    if model.provider is InferenceProvider.GOOGLE:
        result: ModelSettings = dict(GOOGLE_RELAXED_SAFETY_SETTINGS)
    else:
        pseudonym = pseudonymous_inference_user(user_id) if user_id else None
        result = dict(openrouter_settings(model, pseudonymous_user=pseudonym))
    if max_tokens is not None:
        result["max_tokens"] = max_tokens
    return result


# Compatibility export while call sites migrate to provider-aware settings.
RELAXED_SAFETY_SETTINGS = GOOGLE_RELAXED_SAFETY_SETTINGS
