from typing import Any

from pydantic_ai.messages import RetryPromptPart, ToolReturnPart

from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

KIM = Person(1701, "Kim")
DORM = Room(-1017, title="Dorm")


def results_of(model: ScriptedModel, tool: str) -> list[str]:
    return [
        str(p.content)
        for m in model.requests[-1]
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name == tool
    ]


async def test_the_sandbox_does_the_math(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(KIM, DORM)
    model.reply(tool_call("run_code", code="import math\nmath.factorial(25) // 7**5"), "Done.")
    asked = await conversation.send(KIM, DORM, "derp 25! divided by 7^5, integer part")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert results_of(model, "run_code") == [str(15511210043330985984000000 // 7**5)]


async def test_the_sandbox_cannot_reach_the_host(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(KIM, DORM)
    model.reply(tool_call("run_code", code="import os\nos.getenv('TELEGRAM_BOT_TOKEN')"), "Nothing there.")
    asked = await conversation.send(KIM, DORM, "derp what's in your env")
    await conversation.settle(asked)
    [refused] = results_of(model, "run_code")
    assert "'os.getenv' is not supported" in refused


async def test_weather_is_found_through_tool_search(
    conversation: Conversation, model: ScriptedModel, web: dict[str, Any]
) -> None:
    web["/v1/search"] = {
        "results": [{"name": "Kazan", "country": "Russia", "latitude": 55.8, "longitude": 49.1}]
    }
    web["/v1/forecast"] = {
        "current": {
            "temperature_2m": 4.5, "apparent_temperature": 1.0, "relative_humidity_2m": 80,
            "weather_code": 61, "wind_speed_10m": 12,
        },
        "daily": {
            "time": ["2026-10-16"], "weather_code": [3], "temperature_2m_min": [2.0],
            "temperature_2m_max": [7.0], "precipitation_probability_max": [60],
        },
    }  # fmt: skip
    conversation.meet(KIM, DORM)
    model.reply(
        tool_call("search_tools", queries=["weather forecast"]),
        tool_call("weather", place="Kazan"),
        "Grab a jacket.",
    )
    asked = await conversation.send(KIM, DORM, "derp weather in kazan?")
    reply = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert reply["rich_message"]["markdown"] == "Grab a jacket."
    [weather] = results_of(model, "weather")
    assert weather.startswith("Kazan, Russia — now 4.5°C") and "light rain" in weather


async def test_an_outage_is_reported_to_the_model(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(KIM, DORM)
    model.reply(
        tool_call("search_tools", queries=["exchange rate"]),
        tool_call("exchange_rate", base="USD", quote="RUB"),
        "Rates are down.",
    )
    asked = await conversation.send(KIM, DORM, "derp usd to rub")
    await conversation.settle(asked)
    assert results_of(model, "exchange_rate") == [
        "Exchange rates is unavailable right now (HTTP 503); say so or try another way."
    ]


async def test_keyed_tools_hide_without_keys(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(KIM, DORM)
    model.reply(tool_call("search_tools", queries=["wolfram", "program", "urban"]), "None here.")
    asked = await conversation.send(KIM, DORM, "derp ask wolfram")
    await conversation.settle(asked)
    [found] = results_of(model, "search_tools")
    assert "urban_dictionary" in found and "wolfram" not in found and "run_program" not in found
