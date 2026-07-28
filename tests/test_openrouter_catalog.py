"""Pinned OpenRouter catalog, routing, and deterministic selection contracts."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from derp.catalog import (
    OPENROUTER_MODEL_CATALOG,
    ChatSelection,
    DataCollectionPolicy,
    InferenceProvider,
    InputModality,
    ModelRole,
    ModelSelector,
    TokenPricing,
    get_openrouter_model,
)
from derp.llm.providers import openrouter_settings, pseudonymous_inference_user


def test_catalog_uses_canonical_pinned_slugs_and_decimal_prices() -> None:
    assert set(OPENROUTER_MODEL_CATALOG) == set(ModelRole)
    for model in OPENROUTER_MODEL_CATALOG.values():
        assert model.provider is InferenceProvider.OPENROUTER
        assert "/" in model.provider_model_id
        assert not model.provider_model_id.endswith(":latest")
        assert model.routing is not None
        if isinstance(model.pricing, TokenPricing):
            assert all(
                isinstance(rate, Decimal)
                for band in model.pricing.bands
                for rate in (band.input_per_million, band.output_per_million)
            )


def test_private_and_free_routes_have_opposite_explicit_data_policy() -> None:
    paid = get_openrouter_model(ModelRole.CHAT_STANDARD)
    free = get_openrouter_model(ModelRole.FREE_TEXT)
    assert paid.routing is not None
    assert free.routing is not None

    assert paid.routing.zero_data_retention
    assert paid.routing.data_collection is DataCollectionPolicy.DENY
    assert paid.routing.require_parameters
    assert free.routing.zero_data_retention is False
    assert free.routing.data_collection is DataCollectionPolicy.ALLOW
    assert isinstance(free.pricing, TokenPricing)
    assert free.pricing.estimate_usd(input_tokens=1_000, output_tokens=1_000) == 0


def test_image_route_caps_per_image_without_conflating_output_token_units() -> None:
    image = get_openrouter_model(ModelRole.IMAGE)
    assert image.routing is not None
    assert image.routing.max_price is not None

    assert image.routing.max_price.image == Decimal("0.151")
    assert image.routing.max_price.request == Decimal("0.151")


def test_unlisted_dedicated_media_models_fail_closed_at_transport_boundary() -> None:
    for role in (
        ModelRole.TTS,
        ModelRole.STT,
        ModelRole.VIDEO_FAST,
        ModelRole.VIDEO_STANDARD,
    ):
        assert get_openrouter_model(role).available is False


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        (ChatSelection(paid=False), ModelRole.CHAT_ECONOMY),
        (ChatSelection(paid=True), ModelRole.CHAT_STANDARD),
        (
            ChatSelection(
                paid=True,
                modalities=frozenset({InputModality.TEXT, InputModality.AUDIO}),
            ),
            ModelRole.CHAT_MULTIMODAL,
        ),
        (
            ChatSelection(paid=False, free_mode_allowed=True),
            ModelRole.FREE_TEXT,
        ),
        (
            ChatSelection(
                paid=False,
                free_mode_allowed=True,
                modalities=frozenset({InputModality.TEXT, InputModality.IMAGE}),
            ),
            ModelRole.FREE_VISUAL,
        ),
        (
            ChatSelection(
                paid=False,
                free_mode_allowed=True,
                modalities=frozenset({InputModality.TEXT, InputModality.AUDIO}),
            ),
            ModelRole.FREE_AUDIO,
        ),
    ],
)
def test_selector_is_deterministic_and_capability_aware(
    selection: ChatSelection,
    expected: ModelRole,
) -> None:
    assert ModelSelector().select_chat(selection).key is expected


def test_free_video_understanding_uses_reviewed_visual_model() -> None:
    model = ModelSelector().select_chat(
        ChatSelection(
            paid=False,
            free_mode_allowed=True,
            modalities=frozenset({InputModality.TEXT, InputModality.VIDEO}),
        )
    )

    assert model.key is ModelRole.FREE_VISUAL


def test_request_settings_preserve_reviewed_routing_and_pseudonymous_user() -> None:
    model = get_openrouter_model(ModelRole.CHAT_STANDARD)
    pseudonym = pseudonymous_inference_user(UUID(int=42))
    request = openrouter_settings(model, pseudonymous_user=pseudonym)

    assert request["openrouter_provider"] == {
        "order": ["anthropic"],
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "max_price": {"prompt": 2, "completion": 10},
    }
    assert request["openrouter_usage"] == {"include": True}
    assert request["openai_user"] == pseudonym  # type: ignore[typeddict-item]
    assert pseudonym.startswith("derp_")
    assert pseudonym == pseudonymous_inference_user(UUID(int=42))
    assert pseudonym != pseudonymous_inference_user(UUID(int=43))

    economy = openrouter_settings(get_openrouter_model(ModelRole.CHAT_ECONOMY))
    assert economy["openrouter_provider"]["max_price"] == {  # type: ignore[index]
        "prompt": 0.25,
        "completion": 1.5,
    }
