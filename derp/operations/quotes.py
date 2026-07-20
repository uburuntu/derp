"""Pure fixed-price quotes derived from execution plans and catalog rates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Final

from derp.catalog import (
    CATALOG_VERIFIED_ON,
    CREDIT_BASE_USD,
    DEFAULT_MARGIN,
    AudioPricing,
    ImagePricing,
    ImageResolution,
    TokenPricing,
    VideoPricing,
    VideoResolution,
)
from derp.execution import ExecutionPlan, Feature
from derp.operations.types import ContextBand, OperationId, Quote, QuoteId, QuoteKey

PRICING_VERSION: Final = f"google-{CATALOG_VERIFIED_ON.isoformat()}-v1"


def _validate_token_count(value: int, name: str = "input_tokens") -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _validate_positive_int(value: int, name: str) -> None:
    _validate_token_count(value, name)
    if value == 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ChatQuoteInput:
    """Validated usage that selects a base chat context band."""

    input_tokens: int

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)


@dataclass(frozen=True, slots=True)
class InlineChatQuoteInput:
    """Validated usage that selects an inline chat context band."""

    input_tokens: int

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)


@dataclass(frozen=True, slots=True)
class DeepThinkQuoteInput:
    """Validated usage that selects a reasoning context band."""

    input_tokens: int

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)


@dataclass(frozen=True, slots=True)
class ImageGenerateQuoteInput:
    """Validated image-generation usage and requested output size."""

    input_tokens: int
    resolution: ImageResolution

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)
        if not isinstance(self.resolution, ImageResolution):
            raise TypeError("resolution must be an ImageResolution")


@dataclass(frozen=True, slots=True)
class ImageEditQuoteInput:
    """Validated image-edit usage and requested output size."""

    input_tokens: int
    resolution: ImageResolution

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)
        if not isinstance(self.resolution, ImageResolution):
            raise TypeError("resolution must be an ImageResolution")


@dataclass(frozen=True, slots=True)
class TtsQuoteInput:
    """Validated speech usage with a bounded output duration."""

    input_tokens: int
    output_seconds: int

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)
        _validate_positive_int(self.output_seconds, "output_seconds")


@dataclass(frozen=True, slots=True)
class VideoGenerateQuoteInput:
    """Validated video usage with provider-billable output controls."""

    input_tokens: int
    duration_seconds: int
    resolution: VideoResolution

    def __post_init__(self) -> None:
        _validate_token_count(self.input_tokens)
        _validate_positive_int(self.duration_seconds, "duration_seconds")
        if not isinstance(self.resolution, VideoResolution):
            raise TypeError("resolution must be a VideoResolution")


type QuoteInput = (
    ChatQuoteInput
    | InlineChatQuoteInput
    | DeepThinkQuoteInput
    | ImageGenerateQuoteInput
    | ImageEditQuoteInput
    | TtsQuoteInput
    | VideoGenerateQuoteInput
)


@dataclass(frozen=True, slots=True)
class QuotePolicy:
    """Versioned product assumptions layered over immutable provider rates."""

    version: str = PRICING_VERSION
    ttl: timedelta = timedelta(minutes=10)
    credit_value_usd: Decimal = CREDIT_BASE_USD
    margin: Decimal = DEFAULT_MARGIN
    chat_output_tokens: int = 2_048
    inline_output_tokens: int = 1_024
    deep_think_output_tokens: int = 8_192
    image_text_output_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("pricing version must not be blank")
        if self.ttl <= timedelta(0):
            raise ValueError("quote TTL must be positive")
        if not self.credit_value_usd.is_finite() or self.credit_value_usd <= 0:
            raise ValueError("credit value must be a positive finite amount")
        if not self.margin.is_finite() or not Decimal(0) <= self.margin < Decimal(1):
            raise ValueError("margin must be finite, non-negative, and less than one")
        for name in (
            "chat_output_tokens",
            "inline_output_tokens",
            "deep_think_output_tokens",
        ):
            _validate_positive_int(getattr(self, name), name)
        _validate_token_count(
            self.image_text_output_tokens,
            "image_text_output_tokens",
        )

    def credits_for(self, provider_cost_usd: Decimal) -> int:
        """Convert provider cost to credits at this policy's gross margin."""
        if not provider_cost_usd.is_finite() or provider_cost_usd < 0:
            raise ValueError("provider cost must be a non-negative finite amount")
        if provider_cost_usd == 0:
            return 0
        raw_credits = provider_cost_usd / self.credit_value_usd / (1 - self.margin)
        return int(raw_credits.to_integral_value(rounding=ROUND_CEILING))


DEFAULT_QUOTE_POLICY: Final = QuotePolicy()


def _feature_for(quote_input: QuoteInput) -> Feature:
    if isinstance(quote_input, ChatQuoteInput):
        return Feature.CHAT
    if isinstance(quote_input, InlineChatQuoteInput):
        return Feature.INLINE_CHAT
    if isinstance(quote_input, DeepThinkQuoteInput):
        return Feature.DEEP_THINK
    if isinstance(quote_input, ImageGenerateQuoteInput):
        return Feature.IMAGE_GENERATE
    if isinstance(quote_input, ImageEditQuoteInput):
        return Feature.IMAGE_EDIT
    if isinstance(quote_input, TtsQuoteInput):
        return Feature.TTS
    if isinstance(quote_input, VideoGenerateQuoteInput):
        return Feature.VIDEO_GENERATE
    raise TypeError(f"unsupported quote input: {type(quote_input).__name__}")


def _context_envelope_tokens(
    *,
    band: ContextBand,
    model_input_limit: int | None,
) -> int:
    ceiling = {
        ContextBand.SMALL: 8_000,
        ContextBand.MEDIUM: 32_000,
        ContextBand.LARGE: 128_000,
    }.get(band)
    if ceiling is None:
        if model_input_limit is None:
            raise ValueError("maximum context quotes require a model input limit")
        return model_input_limit
    return min(ceiling, model_input_limit) if model_input_limit else ceiling


def _validate_model_input(plan: ExecutionPlan, input_tokens: int) -> ContextBand:
    limit = plan.model.input_token_limit
    if limit is not None and input_tokens > limit:
        raise ValueError(
            f"{input_tokens} input tokens exceed {plan.model.key.value}'s "
            f"{limit}-token limit"
        )
    return ContextBand.for_input_tokens(input_tokens)


def _validate_model_output(plan: ExecutionPlan, output_tokens: int) -> None:
    limit = plan.model.output_token_limit
    if limit is not None and output_tokens > limit:
        raise ValueError(
            f"quote policy output exceeds {plan.model.key.value}'s {limit}-token limit"
        )


def _estimate_provider_cost(
    *,
    plan: ExecutionPlan,
    quote_input: QuoteInput,
    input_envelope_tokens: int,
    policy: QuotePolicy,
) -> tuple[Decimal, str]:
    pricing = plan.model.pricing
    if isinstance(quote_input, ChatQuoteInput):
        if not isinstance(pricing, TokenPricing):
            raise ValueError("chat quotes require token pricing")
        _validate_model_output(plan, policy.chat_output_tokens)
        return (
            pricing.estimate_usd(
                input_tokens=input_envelope_tokens,
                output_tokens=policy.chat_output_tokens,
            ),
            "default",
        )
    if isinstance(quote_input, InlineChatQuoteInput):
        if not isinstance(pricing, TokenPricing):
            raise ValueError("inline chat quotes require token pricing")
        _validate_model_output(plan, policy.inline_output_tokens)
        return (
            pricing.estimate_usd(
                input_tokens=input_envelope_tokens,
                output_tokens=policy.inline_output_tokens,
            ),
            "default",
        )
    if isinstance(quote_input, DeepThinkQuoteInput):
        if not isinstance(pricing, TokenPricing):
            raise ValueError("deep-think quotes require token pricing")
        _validate_model_output(plan, policy.deep_think_output_tokens)
        return (
            pricing.estimate_usd(
                input_tokens=input_envelope_tokens,
                output_tokens=policy.deep_think_output_tokens,
            ),
            "default",
        )
    if isinstance(quote_input, (ImageGenerateQuoteInput, ImageEditQuoteInput)):
        if not isinstance(pricing, ImagePricing):
            raise ValueError("image quotes require image pricing")
        _validate_model_output(plan, policy.image_text_output_tokens)
        return (
            pricing.estimate_usd(
                input_tokens=input_envelope_tokens,
                text_output_tokens=policy.image_text_output_tokens,
                resolution=quote_input.resolution,
            ),
            f"resolution={quote_input.resolution.value}",
        )
    if isinstance(quote_input, TtsQuoteInput):
        if not isinstance(pricing, AudioPricing):
            raise ValueError("TTS quotes require audio pricing")
        _validate_model_output(
            plan,
            pricing.audio_tokens_per_second * quote_input.output_seconds,
        )
        return (
            pricing.estimate_usd(
                input_tokens=input_envelope_tokens,
                output_seconds=quote_input.output_seconds,
            ),
            f"duration={quote_input.output_seconds}s",
        )
    if isinstance(quote_input, VideoGenerateQuoteInput):
        if not isinstance(pricing, VideoPricing):
            raise ValueError("video quotes require video pricing")
        return (
            pricing.estimate_usd(
                duration_seconds=quote_input.duration_seconds,
                resolution=quote_input.resolution,
            ),
            (
                f"duration={quote_input.duration_seconds}s;"
                f"resolution={quote_input.resolution.value}"
            ),
        )
    raise TypeError(f"unsupported quote input: {type(quote_input).__name__}")


@dataclass(frozen=True, slots=True)
class QuoteEngine:
    """Construct fixed quotes without clocks, storage, or provider access."""

    policy: QuotePolicy = DEFAULT_QUOTE_POLICY

    def quote(
        self,
        *,
        quote_id: QuoteId,
        operation_id: OperationId,
        plan: ExecutionPlan,
        quote_input: QuoteInput,
        created_at: datetime,
    ) -> Quote:
        """Price one validated plan using caller-supplied identity and time."""
        if not isinstance(created_at, datetime):
            raise TypeError("created_at must be a datetime")
        expected_feature = _feature_for(quote_input)
        if plan.feature is not expected_feature:
            raise ValueError(
                f"{type(quote_input).__name__} cannot price {plan.feature.value}"
            )

        band = _validate_model_input(plan, quote_input.input_tokens)
        input_envelope_tokens = _context_envelope_tokens(
            band=band,
            model_input_limit=plan.model.input_token_limit,
        )
        provider_cost, variant = _estimate_provider_cost(
            plan=plan,
            quote_input=quote_input,
            input_envelope_tokens=input_envelope_tokens,
            policy=self.policy,
        )
        return Quote(
            id=quote_id,
            operation_id=operation_id,
            key=QuoteKey(
                feature=plan.feature,
                model_key=plan.model.key,
                context_band=band,
                variant=variant,
            ),
            credits=self.policy.credits_for(provider_cost),
            estimated_provider_cost_usd=provider_cost,
            created_at=created_at,
            expires_at=created_at + self.policy.ttl,
            pricing_version=self.policy.version,
            catalog_verified_on=plan.model.pricing_verified_on,
        )


__all__ = [
    "DEFAULT_QUOTE_POLICY",
    "PRICING_VERSION",
    "ChatQuoteInput",
    "DeepThinkQuoteInput",
    "ImageEditQuoteInput",
    "ImageGenerateQuoteInput",
    "InlineChatQuoteInput",
    "QuoteEngine",
    "QuoteInput",
    "QuotePolicy",
    "TtsQuoteInput",
    "VideoGenerateQuoteInput",
]
