"""Typed request and response contracts for OpenRouter's dedicated APIs."""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBytes,
    field_validator,
    model_validator,
)

from derp.openrouter.errors import (
    OpenRouterCostError,
    OpenRouterJobError,
    OpenRouterUsageError,
)

type JsonObject = dict[str, JsonValue]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
UnitIntervalDecimal = Annotated[
    Decimal,
    Field(ge=0, le=1, allow_inf_nan=False),
]

_IDENTIFIER = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
_AUDIO_FORMAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")


class DataCollectionPolicy(StrEnum):
    """OpenRouter provider data-collection routing policy."""

    ALLOW = "allow"
    DENY = "deny"


class ProviderSort(StrEnum):
    """Documented provider ordering strategies."""

    PRICE = "price"
    THROUGHPUT = "throughput"
    LATENCY = "latency"
    EXACTO = "exacto"


class ProviderPartition(StrEnum):
    """How structured provider sorting partitions endpoints."""

    MODEL = "model"
    NONE = "none"


class ImageOutputFormat(StrEnum):
    """Dedicated image response encodings."""

    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"
    SVG = "svg"


class ImageQuality(StrEnum):
    """Dedicated image quality values."""

    AUTO = "auto"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImageBackground(StrEnum):
    """Dedicated image background modes."""

    AUTO = "auto"
    TRANSPARENT = "transparent"
    OPAQUE = "opaque"


class SpeechResponseFormat(StrEnum):
    """Formats currently documented by the dedicated speech endpoint."""

    MP3 = "mp3"
    PCM = "pcm"


class TranscriptionResponseFormat(StrEnum):
    """JSON transcription detail levels."""

    JSON = "json"
    VERBOSE_JSON = "verbose_json"


class TimestampGranularity(StrEnum):
    """Optional transcription timestamp detail."""

    WORD = "word"
    SEGMENT = "segment"


class MediaReferenceKind(StrEnum):
    """Reference modalities accepted by asynchronous video generation."""

    IMAGE = "image_url"
    AUDIO = "audio_url"
    VIDEO = "video_url"


class VideoFrameType(StrEnum):
    """Image-to-video frame positions."""

    FIRST = "first_frame"
    LAST = "last_frame"


class VideoJobStatus(StrEnum):
    """All current OpenRouter video job states."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            VideoJobStatus.COMPLETED,
            VideoJobStatus.FAILED,
            VideoJobStatus.CANCELLED,
            VideoJobStatus.EXPIRED,
        }


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ProviderSortConfig(_RequestModel):
    """Structured provider ordering with optional model partitioning."""

    by: ProviderSort | None = None
    partition: ProviderPartition | None = None


class ProviderRouting(_RequestModel):
    """Shared provider selection, privacy, and passthrough payload."""

    order: tuple[str, ...] | None = None
    only: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    data_collection: DataCollectionPolicy | None = None
    zdr: bool | None = None
    sort: ProviderSort | ProviderSortConfig | None = None
    options: dict[str, JsonObject] | None = Field(default=None, repr=False)

    @field_validator("order", "only", "ignore")
    @classmethod
    def validate_provider_lists(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("provider lists must not be empty")
        for provider in value:
            _require_identifier("provider", provider)
        if len(set(value)) != len(value):
            raise ValueError("provider lists must not contain duplicates")
        return value

    @field_validator("options")
    @classmethod
    def validate_option_keys(
        cls,
        value: dict[str, JsonObject] | None,
    ) -> dict[str, JsonObject] | None:
        for provider in value or ():
            _require_identifier("provider option", provider)
        return value

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)

    def validate_for_image(self) -> None:
        """Reject privacy fields absent from the dedicated image schema."""
        if (
            self.require_parameters is not None
            or self.data_collection is not None
            or self.zdr is not None
        ):
            raise ValueError(
                "image routing does not document privacy or parameter controls"
            )

    def validate_for_options_only(self, surface: str) -> None:
        """Reject routing fields where the endpoint documents options only."""
        if any(
            value is not None
            for value in (
                self.order,
                self.only,
                self.ignore,
                self.allow_fallbacks,
                self.require_parameters,
                self.data_collection,
                self.zdr,
                self.sort,
            )
        ):
            raise ValueError(f"{surface} documents provider options only")


class ImageEndpointParameter(_ResponseModel):
    """One typed capability in the dedicated image endpoint catalog."""

    type: str
    values: tuple[str, ...] = ()
    min: NonNegativeInt | None = None
    max: NonNegativeInt | None = None


class ImageEndpointPrice(_ResponseModel):
    """One unit price exposed by a dedicated image endpoint."""

    billable: str
    unit: str
    cost_usd: NonNegativeDecimal


class ImageModelEndpoint(_ResponseModel):
    """Definitive capabilities and price for one image provider route."""

    provider_name: str
    provider_slug: str
    provider_tag: str
    supported_parameters: dict[str, ImageEndpointParameter]
    allowed_passthrough_parameters: tuple[str, ...] = ()
    supports_streaming: bool = False
    pricing: tuple[ImageEndpointPrice, ...]


class ImageModelEndpoints(_ResponseModel):
    """Live dedicated-image endpoints for one exact model."""

    id: str
    endpoints: tuple[ImageModelEndpoint, ...]


class ZdrEndpointPricing(_ResponseModel):
    """Relevant per-token prices from OpenRouter's live ZDR projection."""

    prompt: NonNegativeDecimal | None = None
    completion: NonNegativeDecimal | None = None
    image_output: NonNegativeDecimal | None = None


class ZdrEndpoint(_ResponseModel):
    """One provider endpoint currently admitted by account-level ZDR."""

    model_id: str
    provider_name: str
    tag: str
    status: int
    pricing: ZdrEndpointPricing


class ZdrEndpoints(_ResponseModel):
    """Account-visible endpoints after OpenRouter applies ZDR policy."""

    data: tuple[ZdrEndpoint, ...]


class MediaReference(_RequestModel):
    """One HTTPS or image-data reference sent to a media provider."""

    kind: MediaReferenceKind
    url: str = Field(repr=False)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str, info: Any) -> str:
        kind = info.data.get("kind")
        if kind is MediaReferenceKind.IMAGE and value.startswith("data:image/"):
            if ";base64," not in value:
                raise ValueError("image data references must be base64 data URLs")
            return value
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("media references must use HTTPS")
        return value

    def to_payload(self) -> JsonObject:
        return {
            "type": self.kind.value,
            self.kind.value: {"url": self.url},
        }


class VideoFrame(_RequestModel):
    """One first or last frame reference for image-to-video generation."""

    frame_type: VideoFrameType
    url: str = Field(repr=False)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        reference = MediaReference(kind=MediaReferenceKind.IMAGE, url=value)
        return reference.url

    def to_payload(self) -> JsonObject:
        return {
            "type": MediaReferenceKind.IMAGE.value,
            "image_url": {"url": self.url},
            "frame_type": self.frame_type.value,
        }


class ModelListQuery(_RequestModel):
    """Bounded subset of current model-catalog filters."""

    offset: NonNegativeInt | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=1000)] | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    supported_parameters: tuple[str, ...] | None = None
    zdr: Literal[True] | None = None

    @field_validator(
        "input_modalities",
        "output_modalities",
        "supported_parameters",
    )
    @classmethod
    def validate_filters(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is not None and not value:
            raise ValueError("model filters must not be empty")
        for item in value or ():
            _require_identifier("model filter", item)
        return value

    def to_params(self) -> dict[str, str | int]:
        params: dict[str, str | int] = {}
        if self.offset is not None:
            params["offset"] = self.offset
        if self.limit is not None:
            params["limit"] = self.limit
        for name in (
            "input_modalities",
            "output_modalities",
            "supported_parameters",
        ):
            if values := getattr(self, name):
                params[name] = ",".join(values)
        if self.zdr:
            params["zdr"] = "true"
        return params


class ImageGenerationRequest(_RequestModel):
    """Buffered dedicated image request; SSE streaming is intentionally absent."""

    model: str
    prompt: str = Field(repr=False)
    n: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    size: str | None = None
    quality: ImageQuality | None = None
    output_format: ImageOutputFormat | None = None
    background: ImageBackground | None = None
    output_compression: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None
    seed: int | None = Field(default=None, strict=True)
    input_references: tuple[MediaReference, ...] = Field(
        default=(), max_length=16, repr=False
    )
    provider: ProviderRouting | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_identifier("model", self.model)
        _require_nonblank("prompt", self.prompt)
        if any(
            reference.kind is not MediaReferenceKind.IMAGE
            for reference in self.input_references
        ):
            raise ValueError("image input references must use image_url")
        if self.provider:
            self.provider.validate_for_image()
        return self

    def to_payload(self) -> JsonObject:
        payload = self.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"input_references", "provider"},
        )
        if self.input_references:
            payload["input_references"] = [
                reference.to_payload() for reference in self.input_references
            ]
        if self.provider:
            payload["provider"] = self.provider.to_payload()
        return payload


class SpeechRequest(_RequestModel):
    """Dedicated text-to-speech request."""

    model: str
    input: str = Field(repr=False)
    voice: str
    response_format: SpeechResponseFormat = SpeechResponseFormat.PCM
    speed: float | None = None
    provider: ProviderRouting | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_identifier("model", self.model)
        _require_nonblank("input", self.input)
        _require_identifier("voice", self.voice)
        if self.speed is not None and (
            not math.isfinite(self.speed) or self.speed <= 0
        ):
            raise ValueError("speed must be finite and positive")
        if self.provider:
            self.provider.validate_for_options_only("speech")
        return self

    def to_payload(self) -> JsonObject:
        payload = self.model_dump(mode="json", exclude_none=True, exclude={"provider"})
        if self.provider:
            payload["provider"] = self.provider.to_payload()
        return payload


class TranscriptionRequest(_RequestModel):
    """Base64 JSON speech-to-text request using bounded caller-owned bytes."""

    model: str
    audio: StrictBytes = Field(repr=False)
    audio_format: str
    language: str | None = None
    response_format: TranscriptionResponseFormat = TranscriptionResponseFormat.JSON
    temperature: float | None = None
    timestamp_granularities: tuple[TimestampGranularity, ...] = ()
    provider: ProviderRouting | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_identifier("model", self.model)
        if not self.audio:
            raise ValueError("audio must not be empty")
        if _AUDIO_FORMAT.fullmatch(self.audio_format) is None:
            raise ValueError("audio_format must be a bounded format label")
        if self.language is not None:
            _require_identifier("language", self.language)
        if self.temperature is not None and (
            not math.isfinite(self.temperature)
            or self.temperature < 0
            or self.temperature > 1
        ):
            raise ValueError("temperature must be between 0 and 1")
        if (
            self.timestamp_granularities
            and self.response_format is not TranscriptionResponseFormat.VERBOSE_JSON
        ):
            raise ValueError("timestamps require verbose_json transcription")
        if self.provider:
            self.provider.validate_for_options_only("transcription")
        return self

    def to_payload(self) -> JsonObject:
        payload = self.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"audio", "audio_format", "provider"},
        )
        payload["input_audio"] = {
            "data": base64.b64encode(self.audio).decode("ascii"),
            "format": self.audio_format,
        }
        if self.provider:
            payload["provider"] = self.provider.to_payload()
        return payload


class VideoGenerationRequest(_RequestModel):
    """Dedicated asynchronous video generation request."""

    model: str
    prompt: str | None = Field(default=None, repr=False)
    duration: PositiveInt | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    size: str | None = None
    generate_audio: bool | None = None
    seed: int | None = Field(default=None, strict=True)
    callback_url: str | None = Field(default=None, repr=False)
    frame_images: tuple[VideoFrame, ...] = Field(default=(), repr=False)
    input_references: tuple[MediaReference, ...] = Field(default=(), repr=False)
    provider: ProviderRouting | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_identifier("model", self.model)
        if self.prompt is not None:
            _require_nonblank("prompt", self.prompt)
        if self.prompt is None and not self.frame_images and not self.input_references:
            raise ValueError("video generation requires a prompt or reference")
        if self.callback_url is not None:
            parsed = urlsplit(self.callback_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("callback_url must use HTTPS")
        frame_types = [frame.frame_type for frame in self.frame_images]
        if len(set(frame_types)) != len(frame_types):
            raise ValueError("video frame positions must be unique")
        if self.provider:
            self.provider.validate_for_options_only("video")
        return self

    def to_payload(self) -> JsonObject:
        payload = self.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"frame_images", "input_references", "provider"},
        )
        if self.frame_images:
            payload["frame_images"] = [
                frame.to_payload() for frame in self.frame_images
            ]
        if self.input_references:
            payload["input_references"] = [
                reference.to_payload() for reference in self.input_references
            ]
        if self.provider:
            payload["provider"] = self.provider.to_payload()
        return payload


class PromptTokenDetails(_ResponseModel):
    audio_tokens: NonNegativeInt | None = None
    cached_tokens: NonNegativeInt | None = None
    cache_write_tokens: NonNegativeInt | None = None


class CompletionTokenDetails(_ResponseModel):
    audio_tokens: NonNegativeInt | None = None
    image_tokens: NonNegativeInt | None = None
    reasoning_tokens: NonNegativeInt | None = None


class _CostedUsage(_ResponseModel):
    cost: NonNegativeDecimal | None = None

    def require_cost(self) -> Decimal:
        if self.cost is None or not self.cost.is_finite():
            raise OpenRouterCostError("OpenRouter usage omitted actual cost")
        return self.cost


class ImageUsage(_CostedUsage):
    prompt_tokens: NonNegativeInt
    completion_tokens: NonNegativeInt
    total_tokens: NonNegativeInt
    prompt_tokens_details: PromptTokenDetails | None = None
    completion_tokens_details: CompletionTokenDetails | None = None
    is_byok: bool | None = None


class TranscriptionUsage(_CostedUsage):
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    seconds: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None


class VideoUsage(_CostedUsage):
    is_byok: bool | None = None


class _ImagePayload(_ResponseModel):
    b64_json: str = Field(repr=False)
    media_type: str | None = None


class _ImageEnvelope(_ResponseModel):
    created: NonNegativeInt
    data: tuple[_ImagePayload, ...]
    usage: ImageUsage | None = None


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """Decoded generated image bytes with an optional reported MIME type."""

    data: bytes = field(repr=False)
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class ImageGenerationResult:
    """Buffered image response and optional accounting identity."""

    created: int
    images: tuple[GeneratedImage, ...] = field(repr=False)
    usage: ImageUsage | None
    generation_id: str | None

    def require_usage(self) -> ImageUsage:
        if self.usage is None:
            raise OpenRouterUsageError("OpenRouter image response omitted usage")
        return self.usage


@dataclass(frozen=True, slots=True)
class SpeechResult:
    """Raw TTS bytes and required generation accounting identity."""

    data: bytes = field(repr=False)
    media_type: str
    generation_id: str


class TranscriptionSegment(_ResponseModel):
    """One timestamped segment from a verbose transcription response."""

    id: NonNegativeInt
    start: NonNegativeDecimal
    end: NonNegativeDecimal
    text: str = Field(repr=False)
    seek: NonNegativeInt | None = None
    tokens: tuple[NonNegativeInt, ...] | None = None
    temperature: NonNegativeDecimal | None = None
    avg_logprob: FiniteDecimal | None = None
    compression_ratio: NonNegativeDecimal | None = None
    no_speech_prob: UnitIntervalDecimal | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.end < self.start:
            raise ValueError("transcription segment end precedes start")
        return self


class TranscriptionWord(_ResponseModel):
    """One timestamped word from a verbose transcription response."""

    word: str = Field(repr=False)
    start: NonNegativeDecimal
    end: NonNegativeDecimal

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.end < self.start:
            raise ValueError("transcription word end precedes start")
        return self


class _TranscriptionEnvelope(_ResponseModel):
    text: str = Field(repr=False)
    usage: TranscriptionUsage | None = None
    task: str | None = None
    language: str | None = None
    duration: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None
    segments: tuple[TranscriptionSegment, ...] = Field(default=(), repr=False)
    words: tuple[TranscriptionWord, ...] = Field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Transcribed text, optional usage, and required generation identity."""

    text: str = field(repr=False)
    usage: TranscriptionUsage | None
    generation_id: str
    task: str | None = None
    language: str | None = None
    duration: Decimal | None = None
    segments: tuple[TranscriptionSegment, ...] = field(default=(), repr=False)
    words: tuple[TranscriptionWord, ...] = field(default=(), repr=False)

    def require_usage(self) -> TranscriptionUsage:
        if self.usage is None:
            raise OpenRouterUsageError("OpenRouter transcription omitted usage")
        return self.usage


class _VideoJobEnvelope(_ResponseModel):
    id: str
    polling_url: str = Field(repr=False)
    status: VideoJobStatus
    generation_id: str | None = None
    unsigned_urls: tuple[str, ...] = Field(default=(), repr=False)
    usage: VideoUsage | None = None
    error: str | None = Field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class VideoJob:
    """Asynchronous video state without retaining remote error text."""

    id: str
    polling_url: str = field(repr=False)
    status: VideoJobStatus
    generation_id: str | None
    unsigned_urls: tuple[str, ...] = field(repr=False)
    usage: VideoUsage | None
    error_present: bool

    def require_completed(self) -> Self:
        if self.status is not VideoJobStatus.COMPLETED:
            raise OpenRouterJobError(job_id=self.id, status=self.status.value)
        if not self.unsigned_urls:
            raise OpenRouterJobError(job_id=self.id, status="completed_without_content")
        return self

    def require_usage(self) -> VideoUsage:
        if self.usage is None:
            raise OpenRouterUsageError("OpenRouter video job omitted usage")
        return self.usage


@dataclass(frozen=True, slots=True)
class VideoContent:
    """Raw video bytes returned by OpenRouter's authenticated content proxy."""

    data: bytes = field(repr=False)
    media_type: str


class ModelArchitecture(_ResponseModel):
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    modality: str | None = None


class ModelPricing(_ResponseModel):
    prompt: NonNegativeDecimal | None
    completion: NonNegativeDecimal | None
    request: NonNegativeDecimal | None = None
    image: NonNegativeDecimal | None = None
    image_output: NonNegativeDecimal | None = None
    image_token: NonNegativeDecimal | None = None
    audio: NonNegativeDecimal | None = None
    audio_output: NonNegativeDecimal | None = None
    input_audio_cache: NonNegativeDecimal | None = None
    input_cache_read: NonNegativeDecimal | None = None
    input_cache_write: NonNegativeDecimal | None = None
    input_cache_write_1h: NonNegativeDecimal | None = None
    internal_reasoning: NonNegativeDecimal | None = None
    web_search: NonNegativeDecimal | None = None

    @field_validator(
        "prompt",
        "completion",
        "request",
        "image",
        "image_output",
        "image_token",
        "audio",
        "audio_output",
        "input_audio_cache",
        "input_cache_read",
        "input_cache_write",
        "input_cache_write_1h",
        "internal_reasoning",
        "web_search",
        mode="before",
    )
    @classmethod
    def normalize_unavailable_price(cls, value: object) -> object:
        """Map OpenRouter's documented -1 router sentinel to unavailable."""
        try:
            return None if Decimal(str(value)) == -1 else value
        except ValueError, ArithmeticError:
            return value


class OpenRouterModel(_ResponseModel):
    id: str
    canonical_slug: str
    name: str
    created: NonNegativeInt
    context_length: NonNegativeInt | None
    architecture: ModelArchitecture
    pricing: ModelPricing
    supported_parameters: tuple[str, ...]
    supported_voices: tuple[str, ...] | None


class _ModelsLinks(_ResponseModel):
    next: str | None


class ModelsPage(_ResponseModel):
    data: tuple[OpenRouterModel, ...]
    total_count: NonNegativeInt
    links: _ModelsLinks


class CreditBalance(_ResponseModel):
    total_credits: NonNegativeDecimal
    total_usage: NonNegativeDecimal

    @property
    def available(self) -> Decimal:
        return self.total_credits - self.total_usage


class _CreditsEnvelope(_ResponseModel):
    data: CreditBalance


class CurrentKeyInfo(_ResponseModel):
    """Spend and limit fields for the authenticated inference key."""

    limit: NonNegativeDecimal | None
    limit_remaining: NonNegativeDecimal | None
    usage: NonNegativeDecimal
    usage_daily: NonNegativeDecimal
    usage_weekly: NonNegativeDecimal
    usage_monthly: NonNegativeDecimal
    is_free_tier: bool
    limit_reset: str | None


class _CurrentKeyEnvelope(_ResponseModel):
    data: CurrentKeyInfo


class GenerationMetadata(_ResponseModel):
    """Generation accounting returned by GET /generation."""

    id: str
    model: str
    created_at: AwareDatetime
    provider_name: str | None
    upstream_id: str | None
    total_cost: NonNegativeDecimal
    usage: NonNegativeDecimal
    upstream_inference_cost: NonNegativeDecimal | None
    is_byok: bool
    cancelled: bool | None
    finish_reason: str | None
    native_finish_reason: str | None
    tokens_prompt: NonNegativeInt | None
    tokens_completion: NonNegativeInt | None
    native_tokens_prompt: NonNegativeInt | None
    native_tokens_completion: NonNegativeInt | None
    native_tokens_cached: NonNegativeInt | None
    native_tokens_reasoning: NonNegativeInt | None
    num_input_audio_prompt: NonNegativeInt | None
    generation_time: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None
    latency: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None


class _GenerationEnvelope(_ResponseModel):
    data: GenerationMetadata


@dataclass(frozen=True, slots=True)
class OpenRouterTimeouts:
    """Explicit per-surface timeout policy for one shared HTTP client."""

    metadata: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(
            connect=5.0,
            read=30.0,
            write=15.0,
            pool=5.0,
        )
    )
    generation: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(
            connect=10.0,
            read=180.0,
            write=60.0,
            pool=10.0,
        )
    )
    video_job: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(
            connect=5.0,
            read=30.0,
            write=15.0,
            pool=5.0,
        )
    )
    media: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(
            connect=10.0,
            read=180.0,
            write=30.0,
            pool=10.0,
        )
    )


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded identifier")


def _require_nonblank(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


__all__ = [
    "CreditBalance",
    "DataCollectionPolicy",
    "GeneratedImage",
    "GenerationMetadata",
    "ImageBackground",
    "ImageGenerationRequest",
    "ImageGenerationResult",
    "ImageEndpointParameter",
    "ImageEndpointPrice",
    "ImageModelEndpoint",
    "ImageModelEndpoints",
    "ImageOutputFormat",
    "ImageQuality",
    "ImageUsage",
    "MediaReference",
    "MediaReferenceKind",
    "ModelListQuery",
    "ModelPricing",
    "ModelsPage",
    "OpenRouterModel",
    "OpenRouterTimeouts",
    "ProviderPartition",
    "ProviderRouting",
    "ProviderSort",
    "ProviderSortConfig",
    "SpeechRequest",
    "SpeechResponseFormat",
    "SpeechResult",
    "TimestampGranularity",
    "TranscriptionRequest",
    "TranscriptionResponseFormat",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionUsage",
    "TranscriptionWord",
    "VideoContent",
    "VideoFrame",
    "VideoFrameType",
    "VideoGenerationRequest",
    "VideoJob",
    "VideoJobStatus",
    "VideoUsage",
    "ZdrEndpoint",
    "ZdrEndpointPricing",
    "ZdrEndpoints",
]
