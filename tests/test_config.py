"""Deployment settings keep operator access explicit and unambiguous."""

import pytest
from pydantic import ValidationError

from derp.config import Settings


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


def test_production_requires_an_explicit_operator() -> None:
    with pytest.raises(ValidationError, match="OPERATOR_IDS"):
        _settings(environment="prod")


@pytest.mark.parametrize("raw", ["0", "-1", "not-an-id", [True], {1.5}])
def test_operator_ids_reject_invalid_values(raw: object) -> None:
    with pytest.raises(ValidationError, match="OPERATOR_IDS"):
        _settings(operator_ids=raw)
