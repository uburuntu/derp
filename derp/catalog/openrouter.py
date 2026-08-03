"""Reviewed OpenRouter model catalog and downstream routing policy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from derp.catalog.google import (
    AudioPricing,
    DataCollectionPolicy,
    ImagePricing,
    ImageResolution,
    InferenceProvider,
    ModelCapability,
    ModelLifecycle,
    ModelRole,
    ModelSpec,
    PriceCeiling,
    RoutingPolicy,
    TokenPriceBand,
    TokenPricing,
    TranscriptionPricing,
    VideoPricing,
    VideoResolution,
)

OPENROUTER_CATALOG_VERIFIED_ON: Final = date(2026, 7, 28)
OPENROUTER_MODELS_URL: Final = "https://openrouter.ai/api/v1/models"
OPENROUTER_PRIVACY_URL: Final = (
    "https://openrouter.ai/docs/features/privacy-and-logging"
)


def _model_url(slug: str) -> str:
    return f"https://openrouter.ai/{slug}"


def _private_route(
    *,
    prompt: str | None = None,
    completion: str | None = None,
    image: str | None = None,
    audio: str | None = None,
    request: str | None = None,
    provider_order: tuple[str, ...] = (),
) -> RoutingPolicy:
    return RoutingPolicy(
        provider_order=provider_order,
        data_collection=DataCollectionPolicy.DENY,
        zero_data_retention=True,
        max_price=PriceCeiling(
            prompt=Decimal(prompt) if prompt else None,
            completion=Decimal(completion) if completion else None,
            image=Decimal(image) if image else None,
            audio=Decimal(audio) if audio else None,
            request=Decimal(request) if request else None,
        ),
    )


_FREE_ROUTE = RoutingPolicy(
    allow_fallbacks=True,
    require_parameters=True,
    data_collection=DataCollectionPolicy.ALLOW,
    zero_data_retention=False,
    max_price=PriceCeiling(
        prompt=Decimal("0"),
        completion=Decimal("0"),
        image=Decimal("0"),
        audio=Decimal("0"),
        request=Decimal("0"),
    ),
)

_VIDEO_ROUTE = RoutingPolicy(
    provider_order=("google-vertex",),
    allow_fallbacks=False,
    require_parameters=True,
    data_collection=DataCollectionPolicy.DENY,
    zero_data_retention=False,
)

_TEXT_TOOLS = frozenset(
    {
        ModelCapability.TEXT_INPUT,
        ModelCapability.TEXT_OUTPUT,
        ModelCapability.TOOLS,
        ModelCapability.THINKING,
        ModelCapability.STRUCTURED_OUTPUT,
    }
)
_VISUAL_TEXT = _TEXT_TOOLS | {
    ModelCapability.IMAGE_INPUT,
    ModelCapability.PDF_INPUT,
}
_OMNI_TEXT = _VISUAL_TEXT | {
    ModelCapability.AUDIO_INPUT,
    ModelCapability.VIDEO_INPUT,
}


def _token_model(
    *,
    role: ModelRole,
    slug: str,
    name: str,
    input_price: str,
    output_price: str,
    capabilities: frozenset[ModelCapability],
    input_limit: int | None,
    output_limit: int | None,
    route: RoutingPolicy,
    lifecycle: ModelLifecycle = ModelLifecycle.STABLE,
    bands: tuple[TokenPriceBand, ...] | None = None,
    canonical_model_id: str | None = None,
) -> ModelSpec:
    return ModelSpec(
        key=role,
        provider_model_id=slug,
        display_name=name,
        lifecycle=lifecycle,
        input_token_limit=input_limit,
        output_token_limit=output_limit,
        capabilities=capabilities,
        pricing=TokenPricing(
            bands=bands
            or (TokenPriceBand(Decimal(input_price), Decimal(output_price)),)
        ),
        documentation_url=_model_url(slug),
        pricing_url=OPENROUTER_MODELS_URL,
        pricing_verified_on=OPENROUTER_CATALOG_VERIFIED_ON,
        provider=InferenceProvider.OPENROUTER,
        routing=route,
        canonical_model_id=canonical_model_id,
    )


_MODELS = (
    _token_model(
        role=ModelRole.CHAT_ECONOMY,
        slug="google/gemini-3.1-flash-lite",
        name="Gemini 3.1 Flash-Lite",
        input_price="0.25",
        output_price="1.50",
        capabilities=_OMNI_TEXT,
        input_limit=1_048_576,
        output_limit=65_536,
        route=_private_route(prompt="0.25", completion="1.50"),
        canonical_model_id="google/gemini-3.1-flash-lite-20260507",
    ),
    _token_model(
        role=ModelRole.CHAT_STANDARD,
        slug="anthropic/claude-sonnet-5",
        name="Claude Sonnet 5",
        input_price="2.00",
        output_price="10.00",
        capabilities=_VISUAL_TEXT,
        input_limit=1_000_000,
        output_limit=128_000,
        route=_private_route(
            prompt="2.00", completion="10.00", provider_order=("anthropic",)
        ),
        canonical_model_id="anthropic/claude-sonnet-5-20260630",
    ),
    _token_model(
        role=ModelRole.CHAT_MULTIMODAL,
        slug="google/gemini-3.5-flash",
        name="Gemini 3.5 Flash",
        input_price="1.50",
        output_price="9.00",
        capabilities=_OMNI_TEXT,
        input_limit=1_048_576,
        output_limit=65_536,
        route=_private_route(prompt="1.50", completion="9.00"),
        canonical_model_id="google/gemini-3.5-flash-20260519",
    ),
    _token_model(
        role=ModelRole.CHAT_REASONING,
        slug="openai/gpt-5.6-sol",
        name="GPT-5.6 SOL",
        input_price="5.00",
        output_price="30.00",
        capabilities=_VISUAL_TEXT,
        input_limit=1_050_000,
        output_limit=128_000,
        route=_private_route(prompt="10.00", completion="45.00"),
        bands=(
            TokenPriceBand(Decimal("5.00"), Decimal("30.00"), 272_000),
            TokenPriceBand(Decimal("10.00"), Decimal("45.00")),
        ),
        canonical_model_id="openai/gpt-5.6-sol-20260709",
    ),
    ModelSpec(
        key=ModelRole.IMAGE,
        provider_model_id="google/gemini-3.1-flash-image",
        display_name="Gemini 3.1 Flash Image",
        lifecycle=ModelLifecycle.STABLE,
        input_token_limit=131_072,
        output_token_limit=32_768,
        capabilities=frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.TEXT_OUTPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.IMAGE_OUTPUT,
                ModelCapability.PDF_INPUT,
                ModelCapability.THINKING,
            }
        ),
        pricing=ImagePricing(
            input_per_million=Decimal("0.50"),
            text_output_per_million=Decimal("3.00"),
            output_usd=(
                (ImageResolution.HALF_K, Decimal("0.045")),
                (ImageResolution.ONE_K, Decimal("0.067")),
                (ImageResolution.TWO_K, Decimal("0.101")),
                (ImageResolution.FOUR_K, Decimal("0.151")),
            ),
            default_resolution=ImageResolution.ONE_K,
            output_token_per_million=Decimal("60.00"),
        ),
        documentation_url=_model_url("google/gemini-3.1-flash-image"),
        pricing_url=OPENROUTER_MODELS_URL,
        pricing_verified_on=OPENROUTER_CATALOG_VERIFIED_ON,
        provider=InferenceProvider.OPENROUTER,
        routing=_private_route(
            prompt="0.50",
            completion="3.00",
            image="0.151",
            request="0.151",
            provider_order=("google-vertex",),
        ),
        canonical_model_id="google/gemini-3.1-flash-image-20260528",
    ),
    ModelSpec(
        key=ModelRole.TTS,
        provider_model_id="google/gemini-3.1-flash-tts-preview",
        display_name="Gemini 3.1 Flash TTS Preview",
        lifecycle=ModelLifecycle.PREVIEW,
        input_token_limit=8_192,
        output_token_limit=16_384,
        capabilities=frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.AUDIO_OUTPUT}
        ),
        pricing=AudioPricing(
            text_input_per_million=Decimal("1.00"),
            audio_output_per_million=Decimal("20.00"),
            audio_tokens_per_second=25,
        ),
        documentation_url=_model_url("google/gemini-3.1-flash-tts-preview"),
        pricing_url=OPENROUTER_MODELS_URL,
        pricing_verified_on=OPENROUTER_CATALOG_VERIFIED_ON,
        provider=InferenceProvider.OPENROUTER,
        routing=_private_route(
            prompt="1.00",
            audio="20.00",
            provider_order=("google-vertex",),
        ),
        available=False,
    ),
    ModelSpec(
        key=ModelRole.STT,
        provider_model_id="openai/whisper-large-v3",
        display_name="Whisper Large V3",
        lifecycle=ModelLifecycle.STABLE,
        input_token_limit=None,
        output_token_limit=None,
        capabilities=frozenset(
            {ModelCapability.AUDIO_INPUT, ModelCapability.TEXT_OUTPUT}
        ),
        pricing=TranscriptionPricing(input_per_minute=Decimal("0.0015")),
        documentation_url=_model_url("openai/whisper-large-v3"),
        pricing_url=OPENROUTER_MODELS_URL,
        pricing_verified_on=OPENROUTER_CATALOG_VERIFIED_ON,
        provider=InferenceProvider.OPENROUTER,
        routing=_private_route(provider_order=("together", "groq")),
        available=False,
    ),
    ModelSpec(
        key=ModelRole.VIDEO_FAST,
        provider_model_id="google/veo-3.1-fast",
        display_name="Veo 3.1 Fast",
        lifecycle=ModelLifecycle.PREVIEW,
        input_token_limit=1_024,
        output_token_limit=None,
        capabilities=frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.VIDEO_OUTPUT,
            }
        ),
        pricing=VideoPricing(
            output_per_second=(
                (VideoResolution.HD_720P, Decimal("0.10")),
                (VideoResolution.HD_1080P, Decimal("0.12")),
                (VideoResolution.UHD_4K, Decimal("0.30")),
            ),
            default_resolution=VideoResolution.HD_720P,
            default_duration_seconds=6,
            supported_durations_seconds=frozenset({4, 6, 8}),
            output_per_second_without_audio=(
                (VideoResolution.HD_720P, Decimal("0.08")),
                (VideoResolution.HD_1080P, Decimal("0.10")),
                (VideoResolution.UHD_4K, Decimal("0.25")),
            ),
        ),
        documentation_url=_model_url("google/veo-3.1-fast"),
        pricing_url=OPENROUTER_MODELS_URL,
        pricing_verified_on=OPENROUTER_CATALOG_VERIFIED_ON,
        provider=InferenceProvider.OPENROUTER,
        routing=_VIDEO_ROUTE,
        retention_exception="OpenRouter video generation temporarily retains job media.",
        available=False,
        canonical_model_id="google/veo-3.1-fast-20260320",
    ),
    ModelSpec(
        key=ModelRole.VIDEO_STANDARD,
        provider_model_id="google/veo-3.1",
        display_name="Veo 3.1",
        lifecycle=ModelLifecycle.PREVIEW,
        input_token_limit=1_024,
        output_token_limit=None,
        capabilities=frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.VIDEO_OUTPUT,
            }
        ),
        pricing=VideoPricing(
            output_per_second=(
                (VideoResolution.HD_720P, Decimal("0.40")),
                (VideoResolution.HD_1080P, Decimal("0.40")),
                (VideoResolution.UHD_4K, Decimal("0.60")),
            ),
            default_resolution=VideoResolution.HD_720P,
            default_duration_seconds=6,
            supported_durations_seconds=frozenset({4, 6, 8}),
            output_per_second_without_audio=(
                (VideoResolution.HD_720P, Decimal("0.20")),
                (VideoResolution.HD_1080P, Decimal("0.20")),
                (VideoResolution.UHD_4K, Decimal("0.40")),
            ),
        ),
        documentation_url=_model_url("google/veo-3.1"),
        pricing_url=OPENROUTER_MODELS_URL,
        pricing_verified_on=OPENROUTER_CATALOG_VERIFIED_ON,
        provider=InferenceProvider.OPENROUTER,
        routing=_VIDEO_ROUTE,
        retention_exception="OpenRouter video generation temporarily retains job media.",
        available=False,
        canonical_model_id="google/veo-3.1-20260320",
    ),
    _token_model(
        role=ModelRole.FREE_TEXT,
        slug="nvidia/nemotron-3-ultra-550b-a55b:free",
        name="Nemotron 3 Ultra 550B A55B (free)",
        input_price="0",
        output_price="0",
        capabilities=_TEXT_TOOLS,
        input_limit=1_000_000,
        output_limit=65_536,
        route=_FREE_ROUTE,
        canonical_model_id="nvidia/nemotron-3-ultra-550b-a55b-20260604",
    ),
    _token_model(
        role=ModelRole.FREE_VISUAL,
        slug="google/gemma-4-31b-it:free",
        name="Gemma 4 31B IT (free)",
        input_price="0",
        output_price="0",
        capabilities=_VISUAL_TEXT | {ModelCapability.VIDEO_INPUT},
        input_limit=262_144,
        output_limit=32_768,
        route=_FREE_ROUTE,
        canonical_model_id="google/gemma-4-31b-it-20260402",
    ),
    _token_model(
        role=ModelRole.FREE_AUDIO,
        slug="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        name="Nemotron 3 Nano Omni 30B A3B (free)",
        input_price="0",
        output_price="0",
        capabilities=_OMNI_TEXT,
        input_limit=256_000,
        output_limit=65_536,
        route=_FREE_ROUTE,
        canonical_model_id=("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-20260428"),
    ),
)


def _build_catalog() -> tuple[Mapping[ModelRole, ModelSpec], Mapping[str, ModelSpec]]:
    by_role = {model.key: model for model in _MODELS}
    by_id = {model.provider_model_id: model for model in _MODELS}
    if len(by_role) != len(_MODELS) or len(by_id) != len(_MODELS):
        raise ValueError("OpenRouter model roles and provider IDs must be unique")
    return MappingProxyType(by_role), MappingProxyType(by_id)


OPENROUTER_MODEL_CATALOG, _OPENROUTER_MODELS_BY_ID = _build_catalog()


def get_openrouter_model(role: ModelRole) -> ModelSpec:
    """Resolve a semantic role to the reviewed OpenRouter model."""
    return OPENROUTER_MODEL_CATALOG[role]


def get_openrouter_model_by_id(provider_model_id: str) -> ModelSpec:
    """Resolve a canonical OpenRouter slug."""
    return _OPENROUTER_MODELS_BY_ID[provider_model_id]
