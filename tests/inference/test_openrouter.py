import base64
import json
from decimal import Decimal
from typing import Any

import httpx
import httpx2
import pytest
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import RequestUsage

from derp.config import Settings
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.openrouter import (
    chat_model,
    chat_settings,
    conversation_id,
    provider_config,
    response_usage,
    total_cost,
)
from derp.inference.roles import DEFAULT_PINS, ModelRole, RolePin


def test_zdr_is_per_request_except_free() -> None:
    assert provider_config(DEFAULT_PINS[ModelRole.CHAT_SPONSORED]) == {"zdr": True, "data_collection": "deny"}
    assert provider_config(DEFAULT_PINS[ModelRole.CHAT_FREE]) == {}


def test_a_pins_provider_allowlist_narrows_its_zdr_routes() -> None:
    assert provider_config(DEFAULT_PINS[ModelRole.MEDIA]) == {
        "zdr": True,
        "data_collection": "deny",
        "only": ["google-vertex"],
    }


def completion(text: str) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "google/gemini-3.5-flash-lite",
        "provider": "Google",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 50, "total_tokens": 750, "cost": 0.0003},
    }


@pytest.mark.parametrize(
    ("media_type", "part"),
    [
        ("video/webm", {"type": "video_url", "video_url": {"url": "data:video/webm;base64,AAEC"}}),
        ("audio/ogg", {"type": "input_audio", "input_audio": {"data": "AAEC", "format": "ogg"}}),
        ("audio/mp4", {"type": "input_audio", "input_audio": {"data": "AAEC", "format": "m4a"}}),
        (
            "application/pdf",
            {
                "type": "file",
                "file": {"file_data": "data:application/pdf;base64,AAEC", "filename": "filename.pdf"},
            },
        ),
    ],
)
async def test_media_pin_requests_carry_the_file_in_openrouters_shapes(
    media_type: str, part: dict[str, Any]
) -> None:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=completion("a clip\n\n0:01 hi"))

    pin = DEFAULT_PINS[ModelRole.MEDIA]
    pdf = media_type == "application/pdf"
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = Agent(
            chat_model(pin, OpenRouterProvider(api_key="test", http_client=client)),
            model_settings=chat_settings(pin, max_tokens=100, documents=pdf),
        )
        result = await agent.run(["what happens?", BinaryContent(b"\x00\x01\x02", media_type=media_type)])
    assert result.output == "a clip\n\n0:01 hi"
    (body,) = bodies
    assert body["messages"][-1]["content"][-1] == part
    assert body["provider"]["only"] == ["google-vertex"] and body["provider"]["zdr"] is True
    assert body.get("plugins") == ([{"id": "file-parser", "pdf": {"engine": "native"}}] if pdf else None)
    assert response_usage(result.response).downstream_provider == "Google"


def test_fast_pins_prefer_fast_providers_softly() -> None:
    config = dict(provider_config(DEFAULT_PINS[ModelRole.CHAT]))
    assert config["preferred_min_throughput"] == {"p50": 80}
    assert config["preferred_max_latency"] == {"p50": 1.5}
    assert "max_price" not in config and "sort" not in config  # a preference, never a filter


def test_chat_settings_carry_fallbacks_and_max_price() -> None:
    pin = RolePin(model="a/b", fallbacks=("c/d",), max_price_prompt=1.5, max_price_completion=6)
    settings = chat_settings(pin, max_tokens=2000)
    assert settings.get("openrouter_models") == ["c/d"]
    assert settings.get("max_tokens") == 2000
    assert settings.get("openrouter_provider", {}).get("max_price") == {"prompt": 1.5, "completion": 6}
    assert "openrouter_models" not in chat_settings(RolePin(model="a/b"), max_tokens=10)


def test_conversation_ids_make_routing_sticky_without_exposing_ids() -> None:
    chat = conversation_id("chat:-1001:0")
    assert chat == conversation_id("chat:-1001:0") != conversation_id("chat:-1001:7")
    assert "1001" not in chat and len(chat) == 32
    settings = chat_settings(RolePin(model="a/b"), max_tokens=10, session=chat)
    assert settings.get("extra_body") == {"session_id": chat}
    assert "extra_body" not in chat_settings(RolePin(model="a/b"), max_tokens=10)


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
    with pytest.raises(ValueError, match="no ZDR host"):
        await api.generate_image("m/y", "a dog")


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


async def test_missing_cost_falls_back_to_the_configured_estimate() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images"):
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"PNG").decode()}]})
        return httpx.Response(200, json={"text": "hi", "usage": {"seconds": 90}})

    api = api_with(httpx.MockTransport(handle))
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    image = await api.generate_image("m/x", "a cat", only=("google-vertex",))
    assert image.cost_usd == Decimal(str(defaults.image_price_usd))
    transcript = await api.transcribe("m/stt", b"OggS", "ogg", seconds=90)
    assert transcript.cost_usd == Decimal(str(defaults.stt_price_usd_per_minute)) * Decimal("1.5")


async def test_speak_returns_mp3_priced_per_character_under_zdr() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["voice"] == "nobody":
            return httpx.Response(400, json={"error": {"message": 'Unknown voice "nobody".'}})
        if body["voice"] == "mute":
            return httpx.Response(200, content=b"", headers={"content-type": "audio/mpeg"})
        seen["path"] = request.url.path
        seen.update(body)
        return httpx.Response(
            200, content=b"ID3mp3", headers={"content-type": "audio/mpeg", "x-generation-id": "gen-tts-1"}
        )

    api = api_with(httpx.MockTransport(handle))
    script = "[shouting] ОПЯТЬ?! [laughs]"
    speech = await api.speak("elevenlabs/eleven-v4-turbo", script, "callum")
    assert seen["path"] == "/api/v1/audio/speech"
    assert seen["response_format"] == "mp3" and seen["input"] == script and seen["voice"] == "callum"
    assert seen["provider"] == {"zdr": True, "data_collection": "deny"}
    assert speech.data == b"ID3mp3" and speech.generation_id == "gen-tts-1"
    assert speech.characters == len(script)
    assert speech.cost_usd == Decimal("0.00002") * len(script)  # $20 per 1M characters, the list price
    unlisted = await api.speak("someone/new-tts", "hi", "callum")
    assert unlisted.cost_usd == Decimal("0.00008")  # priced as the dearest listed model, never free
    with pytest.raises(OpenRouterError) as caught:
        await api.speak("elevenlabs/eleven-v4-turbo", "x", "nobody")
    assert caught.value.status == 400 and not caught.value.retryable
    with pytest.raises(OpenRouterError, match="no audio"):
        await api.speak("elevenlabs/eleven-v4-turbo", "x", "mute")


async def test_decide_asks_the_systemone_endpoint_under_zdr() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen.update(json.loads(request.content))
        answers = {"addressed": {"type": "noul", "noul": 0.91}}
        usage = {"input_tokens": 350, "output_tokens": 35, "cost": 1.47e-05}
        return httpx.Response(
            200, json={"model": "typesafe/jev-1.14-20261014", "answers": answers, "usage": usage}
        )

    api = api_with(httpx.MockTransport(handle))
    question = {"type": "noul", "instructions": "Is `new_message` speaking to Derp?"}
    decided = await api.decide("typesafe/jev-1.14", {"new_message": "derp, hi"}, {"addressed": question})
    assert seen["path"] == "/api/v1/systemone"
    assert seen["provider"] == {"zdr": True, "data_collection": "deny"}
    assert decided.answers["addressed"]["noul"] == 0.91 and decided.cost_usd == Decimal("0.0000147")
    assert decided.model == "typesafe/jev-1.14-20261014" and decided.input_tokens == 350
    with pytest.raises(OpenRouterError, match="do not match"):  # every question must come back answered
        await api.decide("typesafe/jev-1.14", "x", {"addressed": question, "extra": question})


async def test_a_decision_without_a_cost_is_priced_from_its_tokens() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        answers = {"q": {"type": "noul", "noul": 0.1}}
        return httpx.Response(
            200, json={"model": "m", "answers": answers, "usage": {"input_tokens": 1_000_000}}
        )

    decided = await api_with(httpx.MockTransport(handle)).decide("m", "x", {"q": {"type": "noul"}})
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert decided.cost_usd == Decimal(str(defaults.decisions.price_usd_per_m_input))


def test_a_deeper_effort_raises_reasoning_and_its_room_but_never_lowers_it() -> None:
    pin = RolePin(model="a/b")  # reasoning "low"
    assert chat_settings(pin, max_tokens=2000).get("openrouter_reasoning") == {
        "effort": "low",
        "exclude": True,
    }
    high = chat_settings(pin, max_tokens=2000, effort="high")
    assert high.get("openrouter_reasoning") == {"effort": "high", "exclude": True}
    assert high.get("max_tokens") == 8000  # reasoning shares max_tokens with the answer
    assert chat_settings(pin, max_tokens=2000, effort="medium").get("max_tokens") == 4000
    deep = chat_settings(RolePin(model="a/b", reasoning="high"), max_tokens=10, effort="medium")
    assert deep.get("openrouter_reasoning") == {"effort": "high", "exclude": True}
    unset = chat_settings(RolePin(model="a/b", reasoning=None), max_tokens=10, effort="high")
    assert "openrouter_reasoning" not in unset and unset.get("max_tokens") == 10
