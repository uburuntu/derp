from decimal import Decimal

import pytest

from derp.config import MissingIntegrationError, Settings
from derp.inference.roles import ModelRole
from derp.money.amounts import format_dc


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_secrets_never_in_repr() -> None:
    settings = make(telegram_bot_token="123:SECRET", openrouter_api_key="sk-or-secret")
    assert "SECRET" not in repr(settings)
    assert "sk-or-secret" not in repr(settings)
    assert set(settings.secret_values()) == {"123:SECRET", "sk-or-secret"}


def test_credits_for_usd_keeps_fractional_pricing_after_markup() -> None:
    settings = make()
    assert settings.credits_for_usd(0) == 0
    assert settings.credits_for_usd(0.001) == Decimal("1.2")
    assert settings.credits_for_usd(0.0025) == 3  # exactly 3.0 dc stays 3
    assert settings.credits_for_usd(0.039) == Decimal("46.8")
    assert settings.credits_for_usd(Decimal("0.000000011")) == Decimal("0.000013")


def test_display_preserves_the_smallest_and_largest_coin_amounts() -> None:
    assert format_dc(Decimal("0.000001")) == "0.000001"
    assert format_dc(Decimal("15432.120000")) == "15,432.12"
    assert format_dc(9_223_372_036_854_775_807) == "9,223,372,036,854,775,807"


def test_validate_core_reports_missing_names() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        make().validate_core()
    with pytest.raises(ValueError, match="OWNER_IDS"):
        make(
            environment="prod", telegram_bot_token="t", database_url="d", openrouter_api_key="k"
        ).validate_core()


def test_require_optional_integration() -> None:
    settings = make(wolfram_app_id="abc")
    assert settings.require("wolfram_app_id") == "abc"
    with pytest.raises(MissingIntegrationError):
        settings.require("jdoodle_client_id", "jdoodle_client_secret")


def test_model_pins_default_and_free_role_is_not_zdr() -> None:
    pins = make().model_pins
    assert (
        pins[ModelRole.CHAT].fast and not pins[ModelRole.CHAT_SPONSORED].fast
    )  # paid answers route for speed
    assert pins[ModelRole.CHAT_FREE].zdr is False
    assert all(pin.zdr for role, pin in pins.items() if role is not ModelRole.CHAT_FREE)


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_IDS", "[28006241]")
    monkeypatch.setenv("RUN_CAPS__TOOL_CALLS", "5")
    settings = make()
    assert settings.owner_ids == [28006241]
    assert settings.run_caps.tool_calls == 5
