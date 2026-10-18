"""Real OpenRouter calls (fractions of a cent). Run with ``pytest -m live``; never in CI."""

import pytest
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


async def test_the_decision_pin_tells_addresses_from_passing_mentions() -> None:
    """The synthetic en/ru set (``tests/agent/name_mentions.json``), asked as for a named trigger, under ZDR."""
    import asyncio
    import json
    from pathlib import Path
    from typing import Any

    from derp.agent.decisions import ADDRESSED, EFFORT
    from derp.inference.api import OpenRouterAPI

    settings = Settings()
    pin = settings.model_pins[ModelRole.DECISION]
    cases: list[dict[str, Any]] = json.loads(
        (Path(__file__).parents[1] / "agent" / "name_mentions.json").read_text()
    )
    api = OpenRouterAPI(settings)

    async def ask(case: dict[str, Any]) -> float:
        before = [f"[12:0{i}] #{100 + i} {who}: {text}" for i, (who, text) in enumerate(case["before"])]
        line = f"[12:0{len(before)}] #{100 + len(before)} Kate: {case['text']}"
        state = {"previous_messages": before, "new_message": line}
        decided = await api.decide(pin.model, state, {"addressed": ADDRESSED, "effort": EFFORT}, zdr=pin.zdr)
        return float(decided.answers["addressed"]["noul"])

    try:
        chances = await asyncio.gather(*(ask(case) for case in cases))
    finally:
        await api.close()
    labels = [bool(case["addressed"]) for case in cases]
    right = sum(
        (p >= settings.decisions.addressed) == label for p, label in zip(chances, labels, strict=True)
    )
    print(f"{right}/{len(cases)}", sorted(zip(chances, labels, strict=True)))
    assert right / len(cases) >= 0.9
