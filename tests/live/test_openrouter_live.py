"""Real OpenRouter calls (fractions of a cent). Run with ``pytest -m live``; never in CI."""

import io
from decimal import Decimal

import pytest
from PIL import Image
from pydantic_ai import Agent

from derp.config import Settings
from derp.inference.openrouter import build_provider, chat_model, chat_settings, response_usage
from derp.inference.roles import ModelRole

pytestmark = pytest.mark.live


@pytest.mark.parametrize("role", [ModelRole.CHAT, ModelRole.CHAT_SPONSORED])
async def test_chat_role_reports_cost_and_served_model(role: ModelRole) -> None:
    settings = Settings()
    pin = settings.model_pins[role]
    agent = Agent(chat_model(pin, build_provider(settings)), model_settings=chat_settings(pin, max_tokens=40))
    result = await agent.run("Reply with exactly one word: pong")
    usage = response_usage(result.response)
    print(role, usage)
    assert usage.cost_usd > 0
    assert usage.model and usage.model.split("/")[0] == pin.model.split("/")[0]
    assert usage.generation_id


async def test_web_search_and_fetch_server_tools_are_accepted_and_billed() -> None:
    from pydantic_ai.capabilities import NativeTool
    from pydantic_ai.native_tools import WebFetchTool, WebSearchTool

    settings = Settings()
    pin = settings.model_pins[ModelRole.CHAT_SPONSORED]
    agent = Agent(
        chat_model(pin, build_provider(settings)),
        model_settings=chat_settings(pin, max_tokens=200),
        capabilities=[
            NativeTool(WebSearchTool(search_context_size="low", max_uses=1)),
            NativeTool(WebFetchTool(max_uses=1, max_content_tokens=2000)),
        ],
    )
    result = await agent.run("Open https://example.com and tell me its page title in five words or fewer.")
    usage = response_usage(result.response)
    print(result.output, usage)
    assert "example" in result.output.lower()
    assert usage.cost_usd > 0


async def test_image_pin_generates_on_its_zdr_host() -> None:
    from derp.inference.api import OpenRouterAPI

    settings = Settings()
    pin = settings.model_pins[ModelRole.IMAGE]
    api = OpenRouterAPI(settings)
    try:
        image = await api.generate_image(
            pin.model, "a small red circle on white", only=pin.only[pin.model], aspect_ratio="1:1"
        )
    finally:
        await api.close()
    print(image.model, image.media_type, len(image.data), image.cost_usd)
    assert (
        image.data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"RIFF")
        and image.cost_usd > 0
    )
    assert image.cost_usd <= settings.image_price_usd


@pytest.mark.parametrize(
    "script", ["[whispers] Okay, between us.", "[laughs] Ладно."], ids=["direction", "sound-russian"]
)
async def test_speech_pin_voices_each_line_on_a_model_able_to_act_it_under_zdr(script: str) -> None:
    from derp.agent.tools.voice import line_for, voicing
    from derp.inference.api import OpenRouterAPI

    settings = Settings()
    pin = settings.model_pins[ModelRole.SPEECH]
    model = voicing(settings.speech, pin, script)[0]
    spec = settings.speech.model(model)
    line = line_for(spec, script, "callum")
    api = OpenRouterAPI(settings)
    try:
        speech = await api.speak(
            model, line.text, line.voice, cost_usd=Decimal("0.0006"), zdr=pin.zdr,
            instructions=line.instructions, audio=spec.audio,
        )  # fmt: skip
    finally:
        await api.close()
    print(speech.model, len(speech.data), speech.cost_usd, speech.generation_id)
    mp3 = speech.data[:3] == b"ID3" or speech.data[:2] in (b"\xff\xfb", b"\xff\xf3")
    assert mp3 if spec.audio == "mp3" else speech.data[:4] == b"RIFF"
    assert speech.cost_usd > 0 and speech.generation_id


@pytest.mark.parametrize("role", [ModelRole.CHAT, ModelRole.CHAT_SPONSORED])
async def test_chat_roles_compute_in_the_sandbox(role: ModelRole) -> None:
    from pydantic_ai.messages import ToolCallPart

    from derp.agent.tools.code import CODE_MODE

    settings = Settings()
    pin = settings.model_pins[role]
    agent = Agent(
        chat_model(pin, build_provider(settings)),
        model_settings=chat_settings(pin, max_tokens=400),
        capabilities=[CODE_MODE],
    )
    result = await agent.run("Using run_code, what is 37**21 mod 1000003? Reply with just the number.")
    calls = [p for m in result.all_messages() for p in m.parts if isinstance(p, ToolCallPart)]
    print(role, result.output, [c.tool_name for c in calls], response_usage(result.response))
    assert any(c.tool_name == "run_code" for c in calls)
    assert str(pow(37, 21, 1000003)) in result.output


async def test_free_chains_with_the_free_router_are_accepted() -> None:
    """The free pin's fallback order (OpenRouter's ``openrouter/free`` among it) is a valid request, rotated too."""
    settings = Settings()
    pin = settings.model_pins[ModelRole.CHAT_FREE]
    for chain in (pin, pin.rotated().rotated()):  # the router last, then the router first
        agent = Agent(
            chat_model(chain, build_provider(settings)), model_settings=chat_settings(chain, max_tokens=400)
        )
        result = await agent.run("Reply with exactly one word: pong")
        print(chain.models, result.output, response_usage(result.response))
        assert result.output.strip()


@pytest.mark.parametrize("provider", ["jev", "openai"])
async def test_reply_rule_classifiers(provider: str) -> None:
    """The two routing providers see the same synthetic positive and negative human messages."""
    from derp.inference.api import OpenRouterAPI

    settings = Settings()
    api = OpenRouterAPI(settings)
    model = (
        settings.decisions.openai_model
        if provider == "openai"
        else settings.model_pins[ModelRole.DECISION].model
    )
    questions = {
        "dog": {
            "type": "noul",
            "instructions": "Is Alex talking about his own dog?",
            "criteria": {"true": "His dog is the topic.", "false": "Anything else."},
        }
    }
    try:
        for text, expected in [
            ("My dog just learned to swim!", True),
            ("Курс евро вырос.", False),
            ("У моей собаки сегодня день рождения", True),
        ]:
            result = await api.decide(
                model, {"new_message": f"Alex: {text}"}, questions, zdr=provider == "jev"
            )
            assert (result.answers["dog"]["noul"] >= settings.decisions.rule_match) == expected
        picture = {
            "red": {
                "type": "noul",
                "instructions": "Does the picture show a red square?",
                "criteria": {"true": "The square is red.", "false": "It is another colour."},
            }
        }
        for colour in ("red", "blue"):
            jpeg = io.BytesIO()
            Image.new("RGB", (320, 320), colour).save(jpeg, "JPEG")
            result = await api.decide(
                model,
                {"new_message": "My picture"},
                picture,
                zdr=provider == "jev",
                image_data=jpeg.getvalue(),
            )
            assert (result.answers["red"]["noul"] >= settings.decisions.rule_match) == (colour == "red")
    finally:
        await api.close()


async def test_the_media_pin_hears_a_clip_on_vertex_with_timestamps() -> None:
    """A synthetic clip (a test pattern with a tone) goes whole to the media pin; served by Vertex, priced."""
    import shutil

    from pydantic_ai import BinaryContent

    from derp.agent.watch import HOW, INSTRUCTIONS
    from tests.e2e.test_effects import clip

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    settings = Settings()
    pin = settings.model_pins[ModelRole.MEDIA]
    agent = Agent(
        chat_model(pin, build_provider(settings)),
        instructions=INSTRUCTIONS + HOW["clip"],
        model_settings=chat_settings(pin, max_tokens=settings.watching.output_tokens),
    )
    result = await agent.run(
        ["The question: what do you hear?", BinaryContent(clip("video"), media_type="video/mp4")]
    )
    usage = response_usage(result.response)
    print(result.output, usage)
    assert usage.downstream_provider == "Google"  # Vertex; AI Studio reports itself as "Google AI Studio"
    assert usage.cost_usd > 0 and "0:0" in result.output
