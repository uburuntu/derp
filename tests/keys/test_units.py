from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart

from derp.agent.guard import refusal
from derp.config import Settings
from derp.inference.api import upstream_cost
from derp.inference.openrouter import response_usage
from derp.keys.failures import classify
from derp.keys.flows import chat_from_ref, chat_ref
from derp.keys.pasted import pasted_key, redact
from derp.keys.register import allowed_models, provider_of
from derp.keys.routing import Prices, cycle_start, key_pin, next_cycle
from derp.keys.vault import seal, unseal


def test_pasted_keys_are_found_whole_and_tell_their_provider() -> None:
    key = "sk-proj-" + "A1b2_C3-" * 6
    assert pasted_key(f"here you go: {key} thanks") == key
    assert pasted_key("sk-short") is None and pasted_key(None) is None
    assert redact(f"use {key} now") == "use [API key removed] now" and redact(None) is None
    assert provider_of("sk-ant-api03-" + "x" * 30) == "anthropic"
    assert provider_of(key) == "openai"


def test_a_key_is_registered_for_its_providers_curated_models_and_image_model(settings: Settings) -> None:
    openai = allowed_models(settings, "openai")
    assert "openai/gpt-6.1-sol" in openai and openai[-1] == settings.keys.images["openai"]
    assert all(m.startswith("anthropic/") for m in allowed_models(settings, "anthropic"))


def test_the_sealed_sub_key_opens_only_with_its_secret() -> None:
    sealed = seal("secret", "sk-or-v1-abc")
    assert b"sk-or" not in sealed and unseal("secret", sealed) == "sk-or-v1-abc"
    with pytest.raises(Exception):  # noqa: B017 - AES-GCM refuses a wrong key
        unseal("other", sealed)
    with pytest.raises(ValueError):
        seal("", "x")


def test_plug_in_links_carry_a_signed_chat_id(settings: Settings) -> None:
    ref = chat_ref(settings, -1001292994444)
    assert len("key-" + ref) <= 64 and ref.replace("-", "").replace("_", "").isalnum()
    assert chat_from_ref(settings, ref) == -1001292994444
    forged = chat_ref(settings.model_copy(update={"keys_secret": "another"}), -100123)
    assert chat_from_ref(settings, forged) is None
    assert chat_from_ref(settings, "!!!") is None


def test_cycles_restart_monthly_on_the_day_the_key_was_added() -> None:
    anchor = date(2026, 1, 31)
    assert cycle_start(anchor, datetime(2026, 3, 5, tzinfo=UTC)) == datetime(2026, 2, 28, tzinfo=UTC)
    assert cycle_start(anchor, datetime(2026, 3, 31, 9, tzinfo=UTC)) == datetime(2026, 3, 31, tzinfo=UTC)
    assert next_cycle(anchor, datetime(2026, 3, 5, tzinfo=UTC)) == datetime(2026, 3, 31, tzinfo=UTC)
    assert cycle_start(date(2026, 11, 4), datetime(2027, 1, 2, tzinfo=UTC)) == datetime(
        2026, 12, 4, tzinfo=UTC
    )
    assert next_cycle(date(2026, 11, 4), datetime(2026, 12, 20, tzinfo=UTC)) == datetime(
        2027, 1, 4, tzinfo=UTC
    )


def test_a_key_pin_serves_one_model_on_its_providers_own_endpoints_without_zdr() -> None:
    pin = key_pin("openai", "openai/gpt-6.1-sol")
    assert (
        pin.models == ("openai/gpt-6.1-sol",)
        and not pin.zdr
        and pin.only == {"openai/gpt-6.1-sol": ("openai",)}
    )
    assert Prices(2, 10).turn(10_000, 300) == Decimal("0.023")


def error(status: int, body: object = None, *, upstream: bool = True) -> ModelHTTPError:
    """An error as OpenRouter relays it: the provider's own carries its name (seen live for a revoked key)."""
    wrapped: dict[str, object] = {"message": str(body or "Provider returned error"), "code": status}
    if upstream:
        wrapped["metadata"] = {"provider_name": "OpenAI", "is_byok": True, "raw": str(body)}
    return ModelHTTPError(status_code=status, model_name="m", body=wrapped)


def test_failures_blame_the_key_only_for_its_providers_errors() -> None:
    assert classify(error(401, "invalid_api_key")) == "dead" and classify(error(403, "invalid key")) == "dead"
    assert classify(error(403, "Input was flagged by moderation", upstream=False)) == "refused"
    assert classify(error(402)) == "exhausted"
    assert classify(error(429, {"error": {"code": "insufficient_quota"}})) == "exhausted"
    assert classify(error(429, "rate limited, slow down")) == "failed"
    assert classify(error(403, "Key limit exceeded", upstream=False)) == "failed"  # the sub-key's own limit
    assert classify(error(402, "Insufficient credits", upstream=False)) == "failed"  # Derp's balance
    assert classify(error(503)) == "failed" and classify(TimeoutError()) == "failed"


def test_a_relayed_out_of_credit_error_exhausts_the_key() -> None:
    def relayed(message: str) -> ModelHTTPError:  # seen live: OpenAI's billing refusal, no provider name
        body = {"message": message, "code": 502, "metadata": {"error_type": "provider_unavailable"}}
        return ModelHTTPError(status_code=502, model_name="m", body=body)

    assert classify(relayed("You have no credits remaining. Add credits to continue")) == "exhausted"
    assert classify(relayed("Provider returned error")) == "failed"  # an ordinary outage


def test_explicit_refusals_are_told_apart_from_answers() -> None:
    said = ModelResponse(parts=[], finish_reason="content_filter", provider_details={"refusal": " Nope. "})
    assert refusal(said) == "Nope."
    native = ModelResponse(parts=[TextPart("I can't help")], provider_details={"finish_reason": "refusal"})
    assert refusal(native) == "I can't help"
    assert refusal(ModelResponse(parts=[TextPart("sure")], finish_reason="stop")) is None


def test_a_keys_provider_charge_is_kept_apart_from_openrouters() -> None:
    byok = ModelResponse(
        parts=[TextPart("hi")],
        provider_details={"cost": 0.0001, "is_byok": True, "upstream_inference_cost": 0.02},
    )
    usage = response_usage(byok)
    assert usage.cost_usd == Decimal("0.0001") and usage.upstream_usd == Decimal("0.02")
    assert response_usage(ModelResponse(parts=[], provider_details={"cost": 0.01})).upstream_usd is None
    assert upstream_cost({"is_byok": True, "cost_details": {"upstream_inference_cost": 0.12}}) == Decimal(
        "0.12"
    )
    assert upstream_cost({"cost": 0.1}) is None and upstream_cost(None) is None


def test_a_keys_turn_with_nothing_owed_to_openrouter_still_counts_as_priced() -> None:
    from derp.agent.runs import unreported

    free_fee = ModelResponse(parts=[], provider_details={"is_byok": True, "upstream_inference_cost": 0.02})
    assert not unreported(free_fee)
    assert unreported(ModelResponse(parts=[], provider_details={"downstream_provider": "OpenAI"}))
    assert not unreported(ModelResponse(parts=[], provider_details={"cost": 0.0}))
