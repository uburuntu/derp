import pytest

from derp.config import MissingIntegrationError, Settings
from derp.inference.roles import ModelRole


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_secrets_never_in_repr() -> None:
    settings = make(telegram_bot_token="123:SECRET", openrouter_api_key="sk-or-secret")
    assert "SECRET" not in repr(settings)
    assert "sk-or-secret" not in repr(settings)
    assert set(settings.secret_values()) == {"123:SECRET", "sk-or-secret"}


def test_credits_for_usd_rounds_up_after_markup() -> None:
    settings = make()
    assert settings.credits_for_usd(0) == 0
    assert settings.credits_for_usd(0.001) == 2  # 1.2 dc → 2
    assert settings.credits_for_usd(0.0025) == 3  # exactly 3.0 dc stays 3
    assert settings.credits_for_usd(0.039) == 47  # one Gemini Flash Lite image


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
    assert pins[ModelRole.CHAT].model == "openai/gpt-6-luna"
    assert pins[ModelRole.CHAT_FREE].zdr is False
    assert all(pin.zdr for role, pin in pins.items() if role is not ModelRole.CHAT_FREE)


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_IDS", "[28006241]")
    monkeypatch.setenv("RUN_CAPS__TOOL_CALLS", "5")
    settings = make()
    assert settings.owner_ids == [28006241]
    assert settings.run_caps.tool_calls == 5
