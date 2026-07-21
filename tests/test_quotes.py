"""Immutable quote construction across every provider pricing shape."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from derp.catalog import (
    GoogleModelKey,
    ImagePricing,
    ImageResolution,
    TokenPricing,
    VideoResolution,
    get_openrouter_model,
)
from derp.execution import Feature, plan_execution
from derp.operations import (
    PRICING_VERSION,
    ChatQuoteInput,
    CompositeImageQuoteInput,
    ContextBand,
    DeepThinkQuoteInput,
    FinishingChatQuoteInput,
    ImageEditQuoteInput,
    ImageFinishingAllowance,
    ImageGenerateQuoteInput,
    ImageQuoteInput,
    InlineChatQuoteInput,
    OperationId,
    Quote,
    QuoteEngine,
    QuoteId,
    QuoteInput,
    QuotePolicy,
    TtsQuoteInput,
    VideoGenerateQuoteInput,
)

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _operation_id(feature: Feature, message_id: int = 1) -> OperationId:
    return OperationId.for_command(feature=feature, chat_id=-100, message_id=message_id)


def _quote(
    feature: Feature,
    model_key: GoogleModelKey,
    quote_input: QuoteInput,
    *,
    engine: QuoteEngine | None = None,
) -> Quote:
    return (engine or QuoteEngine()).quote(
        quote_id=QuoteId(UUID(int=1)),
        operation_id=_operation_id(feature),
        plan=plan_execution(feature, model_key),
        quote_input=quote_input,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("feature", "model_key", "quote_input", "provider_cost", "credits", "variant"),
    [
        (
            Feature.CHAT,
            GoogleModelKey.CHAT_STANDARD,
            ChatQuoteInput(input_tokens=1),
            Decimal("0.03648"),
            53,
            "default",
        ),
        (
            Feature.INLINE_CHAT,
            GoogleModelKey.CHAT_STANDARD,
            InlineChatQuoteInput(input_tokens=1),
            Decimal("0.02624"),
            38,
            "default",
        ),
        (
            Feature.DEEP_THINK,
            GoogleModelKey.CHAT_REASONING,
            DeepThinkQuoteInput(input_tokens=1),
            Decimal("0.28576"),
            409,
            "default",
        ),
        (
            Feature.IMAGE_GENERATE,
            GoogleModelKey.IMAGE,
            ImageGenerateQuoteInput(
                input_tokens=1,
                resolution=ImageResolution.ONE_K,
            ),
            Decimal("0.071"),
            102,
            "resolution=1K",
        ),
        (
            Feature.IMAGE_EDIT,
            GoogleModelKey.IMAGE,
            ImageEditQuoteInput(
                input_tokens=1,
                resolution=ImageResolution.TWO_K,
            ),
            Decimal("0.105"),
            150,
            "resolution=2K",
        ),
        (
            Feature.TTS,
            GoogleModelKey.TTS,
            TtsQuoteInput(input_tokens=1, output_seconds=30),
            Decimal("0.023"),
            33,
            "duration=30s",
        ),
        (
            Feature.VIDEO_GENERATE,
            GoogleModelKey.VIDEO_FAST,
            VideoGenerateQuoteInput(
                input_tokens=1,
                duration_seconds=8,
                resolution=VideoResolution.HD_1080P,
            ),
            Decimal("0.96"),
            1_372,
            "duration=8s;resolution=1080p",
        ),
    ],
)
def test_quotes_cover_every_feature_and_provider_pricing_shape(
    feature: Feature,
    model_key: GoogleModelKey,
    quote_input: QuoteInput,
    provider_cost: Decimal,
    credits: int,
    variant: str,
) -> None:
    quote = _quote(feature, model_key, quote_input)

    assert quote.key.feature is feature
    assert quote.key.model_key is model_key
    assert quote.key.context_band is ContextBand.SMALL
    assert quote.key.variant == variant
    assert quote.estimated_provider_cost_usd == provider_cost
    assert quote.credits == credits
    assert quote.pricing_version == PRICING_VERSION
    assert quote.expires_at == NOW + timedelta(minutes=10)


def test_same_model_feature_and_context_band_have_one_fixed_price() -> None:
    low = _quote(
        Feature.CHAT,
        GoogleModelKey.CHAT_STANDARD,
        ChatQuoteInput(input_tokens=1),
    )
    high = _quote(
        Feature.CHAT,
        GoogleModelKey.CHAT_STANDARD,
        ChatQuoteInput(input_tokens=8_000),
    )
    next_band = _quote(
        Feature.CHAT,
        GoogleModelKey.CHAT_STANDARD,
        ChatQuoteInput(input_tokens=8_001),
    )

    assert low.key == high.key
    assert low.estimated_provider_cost_usd == high.estimated_provider_cost_usd
    assert low.credits == high.credits
    assert next_band.key.context_band is ContextBand.MEDIUM
    assert next_band.credits > high.credits


def test_maximum_context_envelope_is_capped_at_the_model_input_limit() -> None:
    quote = _quote(
        Feature.IMAGE_GENERATE,
        GoogleModelKey.IMAGE,
        ImageGenerateQuoteInput(
            input_tokens=131_072,
            resolution=ImageResolution.ONE_K,
        ),
    )

    assert quote.key.context_band is ContextBand.MAXIMUM
    assert quote.estimated_provider_cost_usd == Decimal("0.132536")


@pytest.mark.parametrize(
    (
        "feature",
        "image_input",
        "finishing_model_key",
        "finishing_input_tokens",
        "image_envelope",
        "finishing_envelope",
        "image_band",
        "finishing_band",
        "provider_cost",
        "credits",
    ),
    [
        (
            Feature.IMAGE_GENERATE,
            ImageGenerateQuoteInput(1, ImageResolution.ONE_K),
            GoogleModelKey.CHAT_ECONOMY,
            8_001,
            8_000,
            32_000,
            ContextBand.SMALL,
            ContextBand.MEDIUM,
            Decimal("0.082072"),
            118,
        ),
        (
            Feature.IMAGE_EDIT,
            ImageEditQuoteInput(8_001, ImageResolution.TWO_K),
            GoogleModelKey.CHAT_STANDARD,
            32_001,
            32_000,
            128_000,
            ContextBand.MEDIUM,
            ContextBand.LARGE,
            Decimal("0.39348"),
            563,
        ),
    ],
)
def test_composite_image_quote_is_exact_image_plus_finishing_cost(
    feature: Feature,
    image_input: ImageQuoteInput,
    finishing_model_key: GoogleModelKey,
    finishing_input_tokens: int,
    image_envelope: int,
    finishing_envelope: int,
    image_band: ContextBand,
    finishing_band: ContextBand,
    provider_cost: Decimal,
    credits: int,
) -> None:
    policy = QuotePolicy()
    image_model = get_openrouter_model(GoogleModelKey.IMAGE)
    finishing_model = get_openrouter_model(finishing_model_key)
    assert isinstance(image_model.pricing, ImagePricing)
    assert isinstance(finishing_model.pricing, TokenPricing)
    expected_image_cost = image_model.pricing.estimate_usd(
        input_tokens=image_envelope,
        text_output_tokens=policy.image_text_output_tokens,
        resolution=image_input.resolution,
    )
    expected_finishing_cost = finishing_model.pricing.estimate_usd(
        input_tokens=finishing_envelope,
        output_tokens=policy.image_finishing.output_tokens,
    )

    quote = QuoteEngine(policy).quote_composite_image(
        quote_id=QuoteId(UUID(int=2)),
        operation_id=_operation_id(feature),
        image_plan=plan_execution(feature, GoogleModelKey.IMAGE),
        finishing_plan=plan_execution(Feature.CHAT, finishing_model_key),
        quote_input=CompositeImageQuoteInput(
            image=image_input,
            finishing=FinishingChatQuoteInput(
                model_key=finishing_model_key,
                input_tokens=finishing_input_tokens,
            ),
        ),
        created_at=NOW,
    )

    expected_provider_cost = expected_image_cost + expected_finishing_cost
    assert expected_provider_cost == provider_cost
    assert quote.estimated_provider_cost_usd == expected_provider_cost
    assert quote.credits == policy.credits_for(provider_cost) == credits
    assert quote.key.context_band is image_band
    assert quote.key.variant == (
        f"resolution={image_input.resolution.value};"
        f"finish={finishing_model_key.value}:{finishing_band.value}:v1"
    )
    assert len(quote.key.variant.encode("utf-8")) <= 64


def test_command_image_quote_does_not_include_finishing_work() -> None:
    command = _quote(
        Feature.IMAGE_GENERATE,
        GoogleModelKey.IMAGE,
        ImageGenerateQuoteInput(1, ImageResolution.ONE_K),
    )

    assert command.estimated_provider_cost_usd == Decimal("0.071")
    assert command.credits == 102
    assert command.key.variant == "resolution=1K"


def test_composite_image_quote_rejects_mismatched_features_and_models() -> None:
    quote_input = CompositeImageQuoteInput(
        image=ImageGenerateQuoteInput(100, ImageResolution.ONE_K),
        finishing=FinishingChatQuoteInput(GoogleModelKey.CHAT_ECONOMY, 100),
    )
    engine = QuoteEngine()

    with pytest.raises(ValueError, match="cannot price image_edit"):
        engine.quote_composite_image(
            quote_id=QuoteId.new(),
            operation_id=_operation_id(Feature.IMAGE_EDIT),
            image_plan=plan_execution(Feature.IMAGE_EDIT, GoogleModelKey.IMAGE),
            finishing_plan=plan_execution(
                Feature.CHAT,
                GoogleModelKey.CHAT_ECONOMY,
            ),
            quote_input=quote_input,
            created_at=NOW,
        )

    with pytest.raises(ValueError, match="model does not match"):
        engine.quote_composite_image(
            quote_id=QuoteId.new(),
            operation_id=_operation_id(Feature.IMAGE_GENERATE),
            image_plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            finishing_plan=plan_execution(
                Feature.CHAT,
                GoogleModelKey.CHAT_STANDARD,
            ),
            quote_input=quote_input,
            created_at=NOW,
        )

    with pytest.raises(ValueError, match="requires a chat execution plan"):
        engine.quote_composite_image(
            quote_id=QuoteId.new(),
            operation_id=_operation_id(Feature.IMAGE_GENERATE),
            image_plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            finishing_plan=plan_execution(
                Feature.INLINE_CHAT,
                GoogleModelKey.CHAT_ECONOMY,
            ),
            quote_input=quote_input,
            created_at=NOW,
        )


def test_composite_finishing_limits_and_allowance_version() -> None:
    oversized_input = CompositeImageQuoteInput(
        image=ImageGenerateQuoteInput(100, ImageResolution.ONE_K),
        finishing=FinishingChatQuoteInput(
            GoogleModelKey.CHAT_ECONOMY,
            1_048_577,
        ),
    )
    with pytest.raises(ValueError, match="exceed chat_economy's 1048576-token limit"):
        QuoteEngine().quote_composite_image(
            quote_id=QuoteId.new(),
            operation_id=_operation_id(Feature.IMAGE_GENERATE),
            image_plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            finishing_plan=plan_execution(
                Feature.CHAT,
                GoogleModelKey.CHAT_ECONOMY,
            ),
            quote_input=oversized_input,
            created_at=NOW,
        )

    policy = QuotePolicy(
        image_finishing=ImageFinishingAllowance(version="v2", output_tokens=65_537)
    )
    with pytest.raises(ValueError, match="policy output exceeds"):
        QuoteEngine(policy).quote_composite_image(
            quote_id=QuoteId.new(),
            operation_id=_operation_id(Feature.IMAGE_GENERATE),
            image_plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            finishing_plan=plan_execution(
                Feature.CHAT,
                GoogleModelKey.CHAT_ECONOMY,
            ),
            quote_input=CompositeImageQuoteInput(
                image=ImageGenerateQuoteInput(100, ImageResolution.ONE_K),
                finishing=FinishingChatQuoteInput(
                    GoogleModelKey.CHAT_ECONOMY,
                    100,
                ),
            ),
            created_at=NOW,
        )


def test_quote_input_must_match_the_execution_feature() -> None:
    with pytest.raises(ValueError, match="cannot price image_generate"):
        _quote(
            Feature.IMAGE_GENERATE,
            GoogleModelKey.IMAGE,
            ChatQuoteInput(input_tokens=100),
        )


def test_quote_rejects_usage_beyond_the_resolved_model_limit() -> None:
    with pytest.raises(ValueError, match="exceed video_fast's 1024-token limit"):
        _quote(
            Feature.VIDEO_GENERATE,
            GoogleModelKey.VIDEO_FAST,
            VideoGenerateQuoteInput(
                input_tokens=1_025,
                duration_seconds=6,
                resolution=VideoResolution.HD_720P,
            ),
        )


def test_provider_variants_are_validated_by_catalog_pricing() -> None:
    with pytest.raises(ValueError, match="Unsupported video duration"):
        _quote(
            Feature.VIDEO_GENERATE,
            GoogleModelKey.VIDEO_FAST,
            VideoGenerateQuoteInput(
                input_tokens=100,
                duration_seconds=7,
                resolution=VideoResolution.HD_720P,
            ),
        )

    with pytest.raises(ValueError, match="policy output exceeds"):
        _quote(
            Feature.TTS,
            GoogleModelKey.TTS,
            TtsQuoteInput(input_tokens=100, output_seconds=656),
        )


def test_policy_controls_margin_credit_value_version_and_expiry() -> None:
    policy = QuotePolicy(
        version="launch-v2",
        ttl=timedelta(minutes=3),
        credit_value_usd=Decimal("0.01"),
        margin=Decimal("0.50"),
    )
    quote = _quote(
        Feature.CHAT,
        GoogleModelKey.CHAT_STANDARD,
        ChatQuoteInput(input_tokens=1),
        engine=QuoteEngine(policy),
    )

    assert policy.credits_for(Decimal("0.0101")) == 3
    assert quote.credits == 8
    assert quote.pricing_version == "launch-v2"
    assert quote.expires_at == NOW + timedelta(minutes=3)


def test_policy_and_inputs_enforce_invariants() -> None:
    with pytest.raises(ValueError, match="version"):
        QuotePolicy(version=" ")
    with pytest.raises(ValueError, match="TTL"):
        QuotePolicy(ttl=timedelta(0))
    with pytest.raises(ValueError, match="credit value"):
        QuotePolicy(credit_value_usd=Decimal("0"))
    with pytest.raises(ValueError, match="margin"):
        QuotePolicy(margin=Decimal("1"))
    with pytest.raises(ValueError, match="provider cost"):
        QuotePolicy().credits_for(Decimal("NaN"))
    with pytest.raises(TypeError, match="integer"):
        ChatQuoteInput(input_tokens=True)
    with pytest.raises(ValueError, match="positive"):
        TtsQuoteInput(input_tokens=10, output_seconds=0)


def test_policy_and_inputs_are_immutable() -> None:
    policy = QuotePolicy()
    quote_input = ChatQuoteInput(input_tokens=100)

    with pytest.raises(FrozenInstanceError):
        policy.margin = Decimal("0")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        quote_input.input_tokens = 200  # type: ignore[misc]


def test_caller_controls_quote_identity_and_timestamp() -> None:
    quote_id = QuoteId(UUID("00000000-0000-0000-0000-000000000123"))
    operation_id = _operation_id(Feature.CHAT, message_id=9)
    quote = QuoteEngine().quote(
        quote_id=quote_id,
        operation_id=operation_id,
        plan=plan_execution(Feature.CHAT, GoogleModelKey.CHAT_ECONOMY),
        quote_input=ChatQuoteInput(input_tokens=100),
        created_at=NOW,
    )

    assert quote.id == quote_id
    assert quote.operation_id == operation_id
    assert quote.created_at == NOW
