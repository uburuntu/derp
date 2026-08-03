"""Deployment settings keep operator access explicit and unambiguous."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from derp.catalog import InferenceProvider
from derp.config import Settings
from derp.execution import Feature


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "dev",
        "telegram_bot_token": "123456:test-token",
        "google_api_paid_key": "test-google-key",
        "logfire_token": "test-logfire-token",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[42, 7]", frozenset({7, 42})),
        ("42, 7", frozenset({7, 42})),
        ({42, 7}, frozenset({7, 42})),
    ],
)
def test_operator_ids_accept_documented_formats(
    raw: object,
    expected: frozenset[int],
) -> None:
    assert _settings(operator_ids=raw).operator_ids == expected


def test_admin_ids_remains_a_deprecated_input_alias() -> None:
    settings = _settings(ADMIN_IDS="42,7")

    assert settings.operator_ids == frozenset({7, 42})
    assert not hasattr(settings, "admin_ids")


def test_development_may_run_without_an_operator() -> None:
    assert _settings().operator_ids == frozenset()


def test_feature_provider_defaults_to_openrouter_with_explicit_rollback() -> None:
    default = _settings(openrouter_api_key="test-openrouter-key")
    rollback = _settings(openrouter_enabled_features=[])

    assert default.inference_provider(Feature.IMAGE_GENERATE) is (
        InferenceProvider.OPENROUTER
    )
    assert rollback.inference_provider(Feature.IMAGE_GENERATE) is (
        InferenceProvider.GOOGLE
    )
    assert default.inference_provider(Feature.TTS) is InferenceProvider.GOOGLE
    assert default.inference_provider(Feature.TRANSCRIBE) is InferenceProvider.GOOGLE
    assert (
        default.inference_provider(Feature.VIDEO_GENERATE) is InferenceProvider.GOOGLE
    )


def test_example_environment_does_not_authorize_a_real_operator() -> None:
    configured = [
        line.strip()
        for line in Path("env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert [line for line in configured if line.startswith("OPERATOR_IDS=")] == [
        "OPERATOR_IDS=[]"
    ]


def test_production_requires_an_explicit_operator() -> None:
    with pytest.raises(ValidationError, match="OPERATOR_IDS"):
        _settings(environment="prod")


def test_production_requires_openrouter_credentials() -> None:
    with pytest.raises(ValidationError, match="OPENROUTER_API_KEY"):
        _settings(environment="prod", operator_ids=[42])


@pytest.mark.parametrize(
    "features",
    [[], ["chat"], ["chat", "inline_chat", "image_generate"]],
)
def test_production_rejects_inference_route_downgrades(
    features: list[str],
) -> None:
    with pytest.raises(ValidationError, match="reviewed production surface"):
        _settings(
            environment="prod",
            operator_ids=[42],
            openrouter_api_key="test-openrouter-key",
            openrouter_enabled_features=features,
        )


def test_production_accepts_the_reviewed_openrouter_surface() -> None:
    configured = _settings(
        environment="prod",
        operator_ids=[42],
        openrouter_api_key="test-openrouter-key",
    )

    assert configured.openrouter_enabled_features == frozenset(
        {
            Feature.CHAT,
            Feature.INLINE_CHAT,
            Feature.IMAGE_GENERATE,
            Feature.IMAGE_EDIT,
        }
    )


@pytest.mark.parametrize("raw", ["0", "-1", "not-an-id", [True], {1.5}])
def test_operator_ids_reject_invalid_values(raw: object) -> None:
    with pytest.raises(ValidationError, match="OPERATOR_IDS"):
        _settings(operator_ids=raw)
