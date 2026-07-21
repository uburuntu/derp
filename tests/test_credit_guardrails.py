"""Guardrails for the shared Google model catalog and legacy credit checks."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from derp.catalog import (
    CATALOG_VERIFIED_ON,
    GOOGLE_MODEL_CATALOG,
    GOOGLE_PRICING_URL,
    AudioPricing,
    GoogleModelKey,
    ImagePricing,
    ModelCapability,
    ModelLifecycle,
    TokenPriceBand,
    TokenPricing,
    VideoPricing,
    calculate_credit_cost,
    get_google_model,
    get_google_model_by_id,
    get_openrouter_model,
)
from derp.credits import service as service_module
from derp.credits.service import CreditService
from derp.credits.tools import TOOL_REGISTRY, get_tool
from derp.credits.types import CreditCheckResult
from derp.execution import Feature, plan_execution
from derp.llm.providers import create_model

EXPECTED_MODELS = {
    GoogleModelKey.CHAT_ECONOMY: "gemini-3.1-flash-lite",
    GoogleModelKey.CHAT_STANDARD: "gemini-3.5-flash",
    GoogleModelKey.CHAT_REASONING: "gemini-3.1-pro-preview",
    GoogleModelKey.IMAGE: "gemini-3.1-flash-image",
    GoogleModelKey.TTS: "gemini-3.1-flash-tts-preview",
    GoogleModelKey.VIDEO_FAST: "veo-3.1-fast-generate-preview",
    GoogleModelKey.VIDEO_STANDARD: "veo-3.1-generate-preview",
}


class TestGoogleModelCatalog:
    def test_catalog_contains_only_current_product_models(self) -> None:
        assert {
            key: model.provider_model_id for key, model in GOOGLE_MODEL_CATALOG.items()
        } == EXPECTED_MODELS

    @pytest.mark.parametrize(
        ("key", "provider_id"),
        [
            (key, EXPECTED_MODELS[key])
            for key in (
                GoogleModelKey.CHAT_ECONOMY,
                GoogleModelKey.CHAT_STANDARD,
                GoogleModelKey.CHAT_REASONING,
                GoogleModelKey.IMAGE,
            )
        ],
    )
    def test_provider_factory_executes_catalog_model(
        self, key: GoogleModelKey, provider_id: str
    ) -> None:
        assert create_model(get_google_model(key)).model_name == provider_id

    def test_reverse_lookup_returns_same_spec_object(self) -> None:
        for model in GOOGLE_MODEL_CATALOG.values():
            assert get_google_model_by_id(model.provider_model_id) is model

    def test_catalog_and_specs_are_immutable(self) -> None:
        model = get_google_model(GoogleModelKey.CHAT_STANDARD)
        with pytest.raises(TypeError):
            GOOGLE_MODEL_CATALOG[GoogleModelKey.CHAT_STANDARD] = model  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            model.display_name = "changed"  # type: ignore[misc]

    def test_catalog_has_a_durable_verification_date_and_sources(self) -> None:
        assert CATALOG_VERIFIED_ON.isoformat() == "2026-07-20"
        for model in GOOGLE_MODEL_CATALOG.values():
            assert model.pricing_verified_on == CATALOG_VERIFIED_ON
            assert model.documentation_url.startswith("https://ai.google.dev/")
            assert model.pricing_url == GOOGLE_PRICING_URL

    def test_retired_and_duplicate_models_are_absent(self) -> None:
        provider_ids = {
            model.provider_model_id for model in GOOGLE_MODEL_CATALOG.values()
        }
        assert "gemini-3-pro-preview" not in provider_ids
        assert "gemini-2.5-flash" not in provider_ids
        assert "gemini-2.5-flash-lite" not in provider_ids
        assert "dall-e-3" not in provider_ids

    def test_text_models_have_exact_limits_and_required_capabilities(self) -> None:
        for key in (
            GoogleModelKey.CHAT_ECONOMY,
            GoogleModelKey.CHAT_STANDARD,
            GoogleModelKey.CHAT_REASONING,
        ):
            model = get_google_model(key)
            assert model.input_token_limit == 1_048_576
            assert model.output_token_limit == 65_536
            assert ModelCapability.TEXT_INPUT in model.capabilities
            assert model.supports_tools
            assert ModelCapability.THINKING in model.capabilities

    def test_media_models_describe_text_inputs_and_veo_limits(self) -> None:
        for key in (
            GoogleModelKey.IMAGE,
            GoogleModelKey.TTS,
            GoogleModelKey.VIDEO_FAST,
            GoogleModelKey.VIDEO_STANDARD,
        ):
            assert ModelCapability.TEXT_INPUT in get_google_model(key).capabilities
        for key in (GoogleModelKey.VIDEO_FAST, GoogleModelKey.VIDEO_STANDARD):
            model = get_google_model(key)
            assert model.input_token_limit == 1_024
            assert model.output_token_limit is None

    def test_only_current_preview_models_are_marked_preview(self) -> None:
        preview_keys = {
            model.key
            for model in GOOGLE_MODEL_CATALOG.values()
            if model.lifecycle is ModelLifecycle.PREVIEW
        }
        assert preview_keys == {
            GoogleModelKey.CHAT_REASONING,
            GoogleModelKey.TTS,
            GoogleModelKey.VIDEO_FAST,
            GoogleModelKey.VIDEO_STANDARD,
        }


class TestCurrentPricing:
    def test_text_prices_match_current_standard_paid_rates(self) -> None:
        economy = get_google_model(GoogleModelKey.CHAT_ECONOMY).pricing
        standard = get_google_model(GoogleModelKey.CHAT_STANDARD).pricing
        reasoning = get_google_model(GoogleModelKey.CHAT_REASONING).pricing
        assert isinstance(economy, TokenPricing)
        assert isinstance(standard, TokenPricing)
        assert isinstance(reasoning, TokenPricing)
        assert (
            economy.bands[0].input_per_million,
            economy.bands[0].output_per_million,
        ) == (
            Decimal("0.25"),
            Decimal("1.50"),
        )
        assert (
            standard.bands[0].input_per_million,
            standard.bands[0].output_per_million,
        ) == (
            Decimal("1.50"),
            Decimal("9.00"),
        )
        assert reasoning.bands == (
            TokenPriceBand(Decimal("2.00"), Decimal("12.00"), 200_000),
            TokenPriceBand(Decimal("4.00"), Decimal("18.00")),
        )

    def test_reasoning_uses_long_context_band_above_200k(self) -> None:
        pricing = get_google_model(GoogleModelKey.CHAT_REASONING).pricing
        assert isinstance(pricing, TokenPricing)
        short = pricing.estimate_usd(input_tokens=200_000, output_tokens=1_000)
        long = pricing.estimate_usd(input_tokens=200_001, output_tokens=1_000)
        assert short == Decimal("0.412")
        assert long == Decimal("0.818004")

    def test_image_price_is_explicit_for_default_1k_output(self) -> None:
        pricing = get_google_model(GoogleModelKey.IMAGE).pricing
        assert isinstance(pricing, ImagePricing)
        assert pricing.estimate_usd(input_tokens=0) == Decimal("0.067")

    def test_tts_price_uses_audio_tokens_per_second(self) -> None:
        pricing = get_google_model(GoogleModelKey.TTS).pricing
        assert isinstance(pricing, AudioPricing)
        assert pricing.audio_tokens_per_second == 25
        assert pricing.estimate_usd(input_tokens=0, output_seconds=30) == Decimal(
            "0.015"
        )

    def test_veo_prices_are_per_generated_second(self) -> None:
        fast = get_google_model(GoogleModelKey.VIDEO_FAST).pricing
        standard = get_google_model(GoogleModelKey.VIDEO_STANDARD).pricing
        assert isinstance(fast, VideoPricing)
        assert isinstance(standard, VideoPricing)
        assert fast.estimate_usd(duration_seconds=6) == Decimal("0.60")
        assert standard.estimate_usd(duration_seconds=6) == Decimal("2.40")
        assert fast.default_duration_seconds == 6
        assert fast.supported_durations_seconds == frozenset({4, 6, 8})

    def test_legacy_credit_estimates_round_up(self) -> None:
        expected = {
            GoogleModelKey.CHAT_ECONOMY: 5,
            GoogleModelKey.CHAT_STANDARD: 30,
            GoogleModelKey.CHAT_REASONING: 40,
            GoogleModelKey.IMAGE: 98,
            GoogleModelKey.TTS: 25,
            GoogleModelKey.VIDEO_FAST: 858,
            GoogleModelKey.VIDEO_STANDARD: 3429,
        }
        actual = {
            key: (
                calculate_credit_cost(
                    get_google_model(key),
                    audio_input_tokens=2_000,
                    audio_output_seconds=30,
                )
                if key is GoogleModelKey.TTS
                else calculate_credit_cost(get_google_model(key))
            )
            for key in expected
        }
        assert actual == expected

    def test_invalid_margin_fails_fast(self) -> None:
        model = get_google_model(GoogleModelKey.CHAT_STANDARD)
        with pytest.raises(ValueError, match="Margin"):
            calculate_credit_cost(model, margin=Decimal("1"))

    def test_pricing_types_reject_invalid_usage_and_configuration(self) -> None:
        audio = get_google_model(GoogleModelKey.TTS).pricing
        video = get_google_model(GoogleModelKey.VIDEO_FAST).pricing
        assert isinstance(audio, AudioPricing)
        assert isinstance(video, VideoPricing)
        with pytest.raises(ValueError, match="duration"):
            audio.estimate_usd(input_tokens=0, output_seconds=0)
        with pytest.raises(ValueError, match="duration"):
            video.estimate_usd(duration_seconds=0)
        with pytest.raises(ValueError, match="final"):
            TokenPricing(
                bands=(
                    TokenPriceBand(Decimal("1"), Decimal("1")),
                    TokenPriceBand(Decimal("2"), Decimal("2")),
                )
            )


class TestToolCatalogParity:
    def test_every_tool_resolves_to_the_shared_catalog(self) -> None:
        for tool in TOOL_REGISTRY.values():
            plan = tool.resolve_plan({})
            if plan is None:
                assert tool.feature is None
                continue
            assert plan.feature is tool.feature
            assert plan.model is get_openrouter_model(tool.model_key)

    def test_provider_free_tools_never_carry_model_cost(self) -> None:
        for tool_name in ("web_search",):
            tool = get_tool(tool_name)
            assert tool.resolve_plan({}) is None
            assert tool.model_credit_cost(None, {}) == 0

    def test_tool_total_cost_includes_current_catalog_estimate(self) -> None:
        tool = get_tool("image_generate")
        plan = tool.resolve_plan({})
        assert plan
        model_cost = calculate_credit_cost(plan.model)
        assert tool.total_cost(model_cost) == tool.base_credit_cost + model_cost

    def test_paid_only_tools_are_marked_premium(self) -> None:
        for tool in TOOL_REGISTRY.values():
            if tool.base_credit_cost > 0 and tool.free_daily_limit == 0:
                assert tool.is_premium


class TestCreditCheckResult:
    def test_free_use_properties(self) -> None:
        result = CreditCheckResult(
            allowed=True,
            plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            source="free",
            credits_to_deduct=0,
            credits_remaining=None,
            free_remaining=0,
        )
        assert result.is_free_use
        assert not result.is_paid
        assert result.model_id == "google/gemini-3.1-flash-image"
        assert result.require_plan().model is result.model

    def test_rejected_has_reason(self) -> None:
        result = CreditCheckResult(
            allowed=False,
            plan=plan_execution(Feature.TTS, GoogleModelKey.TTS),
            source="rejected",
            credits_to_deduct=0,
            credits_remaining=0,
            free_remaining=0,
            reject_reason="Not enough credits",
        )
        assert not result.allowed
        assert result.reject_reason == "Not enough credits"

    @pytest.mark.asyncio
    async def test_deduction_rejects_illegal_result_and_feature_states(self) -> None:
        service = CreditService(MagicMock())
        user = MagicMock()
        chat = MagicMock()
        rejected = CreditCheckResult(
            allowed=False,
            plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            source="rejected",
            credits_to_deduct=0,
            credits_remaining=0,
            free_remaining=0,
            reject_reason="No access",
        )
        with pytest.raises(ValueError, match="rejected"):
            await service.deduct(rejected, user, chat, "image_generate")

        image_generation = CreditCheckResult(
            allowed=True,
            plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
            source="free",
            credits_to_deduct=0,
            credits_remaining=None,
            free_remaining=0,
        )
        with pytest.raises(ValueError, match="cannot settle"):
            await service.deduct(image_generation, user, chat, "image_edit")


@pytest.mark.asyncio
async def test_provider_free_daily_limit_rejects_without_fake_model_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_module, "get_balances", AsyncMock(return_value=(100, 100))
    )
    monkeypatch.setattr(service_module, "get_daily_usage", AsyncMock(return_value=10))
    result = await CreditService(MagicMock()).check_tool_access(
        MagicMock(telegram_id=1, id="user"),
        MagicMock(telegram_id=2, id="chat"),
        "web_search",
    )

    assert not result.allowed
    assert result.plan is None
    assert result.model is None
    assert result.credits_to_deduct == 0
    assert result.reject_reason == "Daily limit reached for web_search"


def test_history_windows_are_product_policy_for_chat_models() -> None:
    from derp.history.service import HISTORY_WINDOWS

    assert HISTORY_WINDOWS[GoogleModelKey.CHAT_ECONOMY].max_turns == 10
    assert HISTORY_WINDOWS[GoogleModelKey.CHAT_STANDARD].max_turns == 100
    assert HISTORY_WINDOWS[GoogleModelKey.CHAT_REASONING].max_turns == 100
    assert HISTORY_WINDOWS[GoogleModelKey.CHAT_STANDARD].max_tokens > (
        HISTORY_WINDOWS[GoogleModelKey.CHAT_ECONOMY].max_tokens
    )
