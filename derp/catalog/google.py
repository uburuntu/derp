"""Immutable Google model catalog shared by execution and billing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

CREDIT_BASE_USD: Final = Decimal("0.001")
DEFAULT_MARGIN: Final = Decimal("0.30")
CATALOG_VERIFIED_ON: Final = date(2026, 7, 20)
GOOGLE_PRICING_URL: Final = "https://ai.google.dev/gemini-api/docs/pricing"


class ModelRole(StrEnum):
    """Stable product roles whose concrete provider models can evolve."""

    CHAT_ECONOMY = "chat_economy"
    CHAT_STANDARD = "chat_standard"
    CHAT_MULTIMODAL = "chat_multimodal"
    CHAT_REASONING = "chat_reasoning"
    IMAGE = "image"
    TTS = "tts"
    STT = "stt"
    VIDEO_FAST = "video_fast"
    VIDEO_STANDARD = "video_standard"
    FREE_TEXT = "free_text"
    FREE_VISUAL = "free_visual"
    FREE_AUDIO = "free_audio"


# Compatibility name for downstream code while provider-neutral roles roll out.
GoogleModelKey = ModelRole


class InferenceProvider(StrEnum):
    """Inference platforms supported by the runtime."""

    OPENROUTER = "openrouter"
    GOOGLE = "google"


class DataCollectionPolicy(StrEnum):
    """Whether downstream providers may retain inputs for training."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PriceCeiling:
    """OpenRouter ceilings: token rates per million, media/request rates per unit."""

    prompt: Decimal | None = None
    completion: Decimal | None = None
    image: Decimal | None = None
    audio: Decimal | None = None
    request: Decimal | None = None

    def __post_init__(self) -> None:
        if any(value is not None and value < 0 for value in self.values()):
            raise ValueError("Price ceilings cannot be negative")

    def values(self) -> tuple[Decimal | None, ...]:
        return self.prompt, self.completion, self.image, self.audio, self.request


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Provider routing rules attached to an immutable model selection."""

    provider_order: tuple[str, ...] = ()
    allow_fallbacks: bool = True
    require_parameters: bool = True
    data_collection: DataCollectionPolicy = DataCollectionPolicy.DENY
    zero_data_retention: bool = True
    max_price: PriceCeiling | None = None

    def __post_init__(self) -> None:
        if len(set(self.provider_order)) != len(self.provider_order):
            raise ValueError("Routing provider order must not contain duplicates")


class ModelLifecycle(StrEnum):
    """Provider lifecycle state relevant to production selection."""

    STABLE = "stable"
    PREVIEW = "preview"


class ModelCapability(StrEnum):
    """Capabilities used by feature planning and fail-fast validation."""

    TEXT_INPUT = "text_input"
    TEXT_OUTPUT = "text_output"
    IMAGE_INPUT = "image_input"
    AUDIO_INPUT = "audio_input"
    VIDEO_INPUT = "video_input"
    PDF_INPUT = "pdf_input"
    IMAGE_OUTPUT = "image_output"
    AUDIO_OUTPUT = "audio_output"
    VIDEO_OUTPUT = "video_output"
    TOOLS = "tools"
    THINKING = "thinking"
    STRUCTURED_OUTPUT = "structured_output"


@dataclass(frozen=True, slots=True)
class TokenPriceBand:
    """Token rates that apply up to an optional input-token threshold."""

    input_per_million: Decimal
    output_per_million: Decimal
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.input_per_million < 0 or self.output_per_million < 0:
            raise ValueError("Token prices cannot be negative")
        if self.max_input_tokens is not None and self.max_input_tokens <= 0:
            raise ValueError("Token threshold must be positive")


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """Standard token pricing, including long-context bands when present."""

    bands: tuple[TokenPriceBand, ...]

    def __post_init__(self) -> None:
        if not self.bands or self.bands[-1].max_input_tokens is not None:
            raise ValueError("Token pricing must end with an unbounded band")
        thresholds: list[int] = []
        for band in self.bands[:-1]:
            if band.max_input_tokens is None:
                raise ValueError("Only the final token pricing band can be unbounded")
            thresholds.append(band.max_input_tokens)
        if thresholds != sorted(set(thresholds)):
            raise ValueError("Token pricing thresholds must be unique and increasing")

    def estimate_usd(self, *, input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts cannot be negative")
        rates = next(
            band
            for band in self.bands
            if band.max_input_tokens is None or input_tokens <= band.max_input_tokens
        )
        return (
            rates.input_per_million * input_tokens
            + rates.output_per_million * output_tokens
        ) / 1_000_000


class ImageResolution(StrEnum):
    """Billable image output sizes used by the selected image model."""

    HALF_K = "0.5K"
    ONE_K = "1K"
    TWO_K = "2K"
    FOUR_K = "4K"


@dataclass(frozen=True, slots=True)
class ImagePricing:
    """Image pricing with explicit output cost by resolution."""

    input_per_million: Decimal
    text_output_per_million: Decimal
    output_usd: tuple[tuple[ImageResolution, Decimal], ...]
    default_resolution: ImageResolution
    output_token_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        if self.input_per_million < 0 or self.text_output_per_million < 0:
            raise ValueError("Image token prices cannot be negative")
        if (
            self.output_token_per_million is not None
            and self.output_token_per_million < 0
        ):
            raise ValueError("Image output token price cannot be negative")
        prices = dict(self.output_usd)
        if len(prices) != len(self.output_usd) or self.default_resolution not in prices:
            raise ValueError("Image prices must be unique and include the default")
        if any(price <= 0 for price in prices.values()):
            raise ValueError("Image output prices must be positive")

    def estimate_usd(
        self,
        *,
        input_tokens: int,
        text_output_tokens: int = 0,
        resolution: ImageResolution | None = None,
    ) -> Decimal:
        if input_tokens < 0 or text_output_tokens < 0:
            raise ValueError("Token counts cannot be negative")
        output_price = dict(self.output_usd)[resolution or self.default_resolution]
        return (
            self.input_per_million * input_tokens
            + self.text_output_per_million * text_output_tokens
        ) / 1_000_000 + output_price


@dataclass(frozen=True, slots=True)
class AudioPricing:
    """Speech pricing based on input text and generated audio duration."""

    text_input_per_million: Decimal
    audio_output_per_million: Decimal
    audio_tokens_per_second: int

    def __post_init__(self) -> None:
        if self.text_input_per_million < 0 or self.audio_output_per_million < 0:
            raise ValueError("Audio token prices cannot be negative")
        if self.audio_tokens_per_second <= 0:
            raise ValueError("Audio token rate must be positive")

    def estimate_usd(self, *, input_tokens: int, output_seconds: int) -> Decimal:
        if input_tokens < 0:
            raise ValueError("Input token count cannot be negative")
        if output_seconds <= 0:
            raise ValueError("Audio duration must be positive")
        output_tokens = output_seconds * self.audio_tokens_per_second
        return (
            self.text_input_per_million * input_tokens
            + self.audio_output_per_million * output_tokens
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class TranscriptionPricing:
    """Speech-to-text pricing based on bounded source duration."""

    input_per_minute: Decimal

    def __post_init__(self) -> None:
        if self.input_per_minute <= 0:
            raise ValueError("Transcription price must be positive")

    def estimate_usd(self, *, input_seconds: int) -> Decimal:
        if input_seconds <= 0:
            raise ValueError("Audio duration must be positive")
        return self.input_per_minute * Decimal(input_seconds) / Decimal(60)


class VideoResolution(StrEnum):
    """Billable Veo output resolutions."""

    HD_720P = "720p"
    HD_1080P = "1080p"
    UHD_4K = "4k"


@dataclass(frozen=True, slots=True)
class VideoPricing:
    """Video pricing based on generated seconds and output resolution."""

    output_per_second: tuple[tuple[VideoResolution, Decimal], ...]
    default_resolution: VideoResolution
    default_duration_seconds: int
    supported_durations_seconds: frozenset[int]
    output_per_second_without_audio: tuple[tuple[VideoResolution, Decimal], ...] = ()

    def __post_init__(self) -> None:
        prices = dict(self.output_per_second)
        if (
            len(prices) != len(self.output_per_second)
            or self.default_resolution not in prices
        ):
            raise ValueError("Video prices must be unique and include the default")
        if any(price <= 0 for price in prices.values()):
            raise ValueError("Video output prices must be positive")
        silent_prices = dict(self.output_per_second_without_audio)
        if self.output_per_second_without_audio and set(silent_prices) != set(prices):
            raise ValueError("Silent video prices must cover the same resolutions")
        if any(price <= 0 for price in silent_prices.values()):
            raise ValueError("Silent video output prices must be positive")
        if (
            not self.supported_durations_seconds
            or any(duration <= 0 for duration in self.supported_durations_seconds)
            or self.default_duration_seconds not in self.supported_durations_seconds
        ):
            raise ValueError("Video durations must be positive and include the default")

    def estimate_usd(
        self,
        *,
        duration_seconds: int | None = None,
        resolution: VideoResolution | None = None,
        generate_audio: bool = True,
    ) -> Decimal:
        duration = (
            self.default_duration_seconds
            if duration_seconds is None
            else duration_seconds
        )
        if duration <= 0:
            raise ValueError("Video duration must be positive")
        if duration not in self.supported_durations_seconds:
            raise ValueError(f"Unsupported video duration: {duration}")
        rates = (
            dict(self.output_per_second)
            if generate_audio or not self.output_per_second_without_audio
            else dict(self.output_per_second_without_audio)
        )
        rate = rates[resolution or self.default_resolution]
        return rate * duration


type ModelPricing = (
    TokenPricing | ImagePricing | AudioPricing | TranscriptionPricing | VideoPricing
)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One executable provider model and the facts needed to price it."""

    key: ModelRole
    provider_model_id: str
    display_name: str
    lifecycle: ModelLifecycle
    input_token_limit: int | None
    output_token_limit: int | None
    capabilities: frozenset[ModelCapability]
    pricing: ModelPricing
    documentation_url: str
    pricing_url: str = GOOGLE_PRICING_URL
    pricing_verified_on: date = CATALOG_VERIFIED_ON
    provider: InferenceProvider = InferenceProvider.GOOGLE
    routing: RoutingPolicy | None = None
    retention_exception: str | None = None
    available: bool = True
    canonical_model_id: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_model_id.strip():
            raise ValueError("Provider model ID is required")
        if self.canonical_model_id is not None and not self.canonical_model_id.strip():
            raise ValueError("Canonical model ID must not be blank")
        if any(
            limit is not None and limit <= 0
            for limit in (self.input_token_limit, self.output_token_limit)
        ):
            raise ValueError("Model token limits must be positive")

        required_capability = {
            ImagePricing: ModelCapability.IMAGE_OUTPUT,
            AudioPricing: ModelCapability.AUDIO_OUTPUT,
            TranscriptionPricing: ModelCapability.AUDIO_INPUT,
            VideoPricing: ModelCapability.VIDEO_OUTPUT,
            TokenPricing: ModelCapability.TEXT_OUTPUT,
        }[type(self.pricing)]
        if required_capability not in self.capabilities:
            raise ValueError(
                f"{self.key} pricing requires {required_capability.value} capability"
            )

    @property
    def supports_tools(self) -> bool:
        return ModelCapability.TOOLS in self.capabilities


GoogleModelSpec = ModelSpec


_TEXT_CAPABILITIES = frozenset(
    {
        ModelCapability.TEXT_INPUT,
        ModelCapability.TEXT_OUTPUT,
        ModelCapability.IMAGE_INPUT,
        ModelCapability.AUDIO_INPUT,
        ModelCapability.VIDEO_INPUT,
        ModelCapability.PDF_INPUT,
        ModelCapability.TOOLS,
        ModelCapability.THINKING,
        ModelCapability.STRUCTURED_OUTPUT,
    }
)

_MODELS = (
    ModelSpec(
        key=ModelRole.CHAT_ECONOMY,
        provider_model_id="gemini-3.1-flash-lite",
        display_name="Gemini 3.1 Flash-Lite",
        lifecycle=ModelLifecycle.STABLE,
        input_token_limit=1_048_576,
        output_token_limit=65_536,
        capabilities=_TEXT_CAPABILITIES,
        pricing=TokenPricing(bands=(TokenPriceBand(Decimal("0.25"), Decimal("1.50")),)),
        documentation_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite",
    ),
    ModelSpec(
        key=ModelRole.CHAT_STANDARD,
        provider_model_id="gemini-3.5-flash",
        display_name="Gemini 3.5 Flash",
        lifecycle=ModelLifecycle.STABLE,
        input_token_limit=1_048_576,
        output_token_limit=65_536,
        capabilities=_TEXT_CAPABILITIES,
        pricing=TokenPricing(bands=(TokenPriceBand(Decimal("1.50"), Decimal("9.00")),)),
        documentation_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash",
    ),
    ModelSpec(
        key=ModelRole.CHAT_REASONING,
        provider_model_id="gemini-3.1-pro-preview",
        display_name="Gemini 3.1 Pro Preview",
        lifecycle=ModelLifecycle.PREVIEW,
        input_token_limit=1_048_576,
        output_token_limit=65_536,
        capabilities=_TEXT_CAPABILITIES,
        pricing=TokenPricing(
            bands=(
                TokenPriceBand(
                    Decimal("2.00"),
                    Decimal("12.00"),
                    max_input_tokens=200_000,
                ),
                TokenPriceBand(Decimal("4.00"), Decimal("18.00")),
            )
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview",
    ),
    ModelSpec(
        key=ModelRole.IMAGE,
        provider_model_id="gemini-3.1-flash-image",
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
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image",
    ),
    ModelSpec(
        key=ModelRole.TTS,
        provider_model_id="gemini-3.1-flash-tts-preview",
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
        documentation_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview",
    ),
    ModelSpec(
        key=ModelRole.VIDEO_FAST,
        provider_model_id="veo-3.1-fast-generate-preview",
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
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models/veo-3.1-generate-preview",
    ),
    ModelSpec(
        key=ModelRole.VIDEO_STANDARD,
        provider_model_id="veo-3.1-generate-preview",
        display_name="Veo 3.1 Standard",
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
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models/veo-3.1-generate-preview",
    ),
)


def _build_catalog() -> tuple[Mapping[ModelRole, ModelSpec], Mapping[str, ModelSpec]]:
    by_key = {model.key: model for model in _MODELS}
    by_id = {model.provider_model_id: model for model in _MODELS}
    if len(by_key) != len(_MODELS) or len(by_id) != len(_MODELS):
        raise ValueError("Google model keys and provider IDs must be unique")
    return MappingProxyType(by_key), MappingProxyType(by_id)


GOOGLE_MODEL_CATALOG, _GOOGLE_MODELS_BY_ID = _build_catalog()


def get_google_model(key: ModelRole) -> ModelSpec:
    """Resolve a semantic product model key to one immutable specification."""
    return GOOGLE_MODEL_CATALOG[key]


def get_google_model_by_id(provider_model_id: str) -> ModelSpec:
    """Resolve a provider model ID without creating a second registry."""
    return _GOOGLE_MODELS_BY_ID[provider_model_id]


def estimate_model_cost_usd(
    model: ModelSpec,
    *,
    average_tokens: int = 2_000,
    audio_input_tokens: int | None = None,
    audio_output_seconds: int | None = None,
    video_duration_seconds: int | None = None,
) -> Decimal:
    """Estimate legacy fixed-cost access until immutable quotes replace it."""
    pricing = model.pricing
    if (
        audio_input_tokens is not None or audio_output_seconds is not None
    ) and not isinstance(pricing, AudioPricing):
        raise ValueError("Audio usage can only price an audio model")
    if video_duration_seconds is not None and not isinstance(pricing, VideoPricing):
        raise ValueError("Video duration can only price a video model")
    if isinstance(pricing, TokenPricing):
        return pricing.estimate_usd(
            input_tokens=average_tokens,
            output_tokens=average_tokens,
        )
    if isinstance(pricing, ImagePricing):
        return pricing.estimate_usd(input_tokens=average_tokens)
    if isinstance(pricing, AudioPricing):
        if audio_output_seconds is None:
            raise ValueError("Audio output duration is required")
        return pricing.estimate_usd(
            input_tokens=(
                average_tokens if audio_input_tokens is None else audio_input_tokens
            ),
            output_seconds=audio_output_seconds,
        )
    return pricing.estimate_usd(duration_seconds=video_duration_seconds)


def calculate_credit_cost(
    model: ModelSpec,
    margin: Decimal = DEFAULT_MARGIN,
    avg_tokens: int = 2_000,
    *,
    audio_input_tokens: int | None = None,
    audio_output_seconds: int | None = None,
    video_duration_seconds: int | None = None,
) -> int:
    """Convert the catalog's default usage estimate into legacy credits."""
    if margin < 0 or margin >= 1:
        raise ValueError("Margin must be at least zero and less than one")
    total_cost = estimate_model_cost_usd(
        model,
        average_tokens=avg_tokens,
        audio_input_tokens=audio_input_tokens,
        audio_output_seconds=audio_output_seconds,
        video_duration_seconds=video_duration_seconds,
    )
    if total_cost <= 0:
        return 1
    credit_amount = total_cost / CREDIT_BASE_USD / (1 - margin)
    return max(1, int(credit_amount.to_integral_value(rounding=ROUND_CEILING)))
