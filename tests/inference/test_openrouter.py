import base64
import json
from decimal import Decimal

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from derp.config import Settings
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.openrouter import chat_settings, provider_config, response_usage, total_cost
from derp.inference.roles import DEFAULT_PINS, ModelRole, RolePin


def test_zdr_is_per_request_except_free() -> None:
    assert provider_config(DEFAULT_PINS[ModelRole.CHAT]) == {"zdr": True, "data_collection": "deny"}
    assert provider_config(DEFAULT_PINS[ModelRole.CHAT_FREE]) == {}


def test_chat_settings_carry_fallbacks_and_max_price() -> None:
    pin = RolePin(model="a/b", fallbacks=("c/d",), max_price_prompt=1.5, max_price_completion=6)
    settings = chat_settings(pin, max_tokens=2000)
    assert settings["openrouter_models"] == ["c/d"]
    assert settings["max_tokens"] == 2000
    assert settings["openrouter_provider"]["max_price"] == {"prompt": 1.5, "completion": 6}  # type: ignore[typeddict-item]
    assert "openrouter_models" not in chat_settings(RolePin(model="a/b"), max_tokens=10)


def test_cost_comes_from_reported_usage_only() -> None:
    response = ModelResponse(
        parts=[TextPart("hi")],
        usage=RequestUsage(input_tokens=100, output_tokens=20, details={"reasoning_tokens": 5}),
        model_name="openai/gpt-6-luna",
        provider_response_id="gen-1",
        provider_details={
            "cost": 0.00042,
            "downstream_provider": "Azure",
            "server_tool_use": {"web_search_requests": 1},
        },
    )
    usage = response_usage(response)
    assert usage.cost_usd == Decimal("0.00042")
    assert (usage.model, usage.downstream_provider, usage.generation_id) == (
        "openai/gpt-6-luna",
        "Azure",
        "gen-1",
    )
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (100, 20, 5)
    assert usage.web_search_requests == 1
    unknown = ModelResponse(parts=[TextPart("x")])
    assert response_usage(unknown).cost_usd == 0
    history = [ModelRequest(parts=[UserPromptPart("q")]), response, response]
    assert total_cost(history) == Decimal("0.00084")


def api_with(handler: httpx.MockTransport) -> OpenRouterAPI:
    settings = Settings(_env_file=None, openrouter_api_key="sk-or-test")  # type: ignore[call-arg]
    return OpenRouterAPI(settings, httpx.AsyncClient(base_url="https://or.test/api/v1/", transport=handler))


async def test_generate_image_pins_providers_and_decodes() -> None:
    seen: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        image = base64.b64encode(b"PNG").decode()
        return httpx.Response(200, json={"data": [{"b64_json": image}], "usage": {"cost": 0.039}})

    api = api_with(httpx.MockTransport(handle))
    image = await api.generate_image(
        "m/x", "a cat", only=("google-vertex",), references=[(b"ref", "image/jpeg")]
    )
    assert image.data == b"PNG" and image.cost_usd == Decimal("0.039") and image.media_type == "image/png"
    assert seen["provider"] == {"only": ["google-vertex"], "allow_fallbacks": False}
    assert seen["input_references"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")  # type: ignore[index]


async def test_transcribe_requests_zdr_and_reports_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "bad":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        assert body["provider"]["zdr"] is True
        assert body["input_audio"]["format"] == "ogg"
        return httpx.Response(200, json={"text": " привет ", "usage": {"cost": 0.00001}})

    api = api_with(httpx.MockTransport(handle))
    transcript = await api.transcribe("openai/whisper-large-v3-turbo", b"OggS", "ogg")
    assert transcript.text == "привет"
    with pytest.raises(OpenRouterError) as caught:
        await api.transcribe("bad", b"OggS", "ogg")
    assert caught.value.retryable
