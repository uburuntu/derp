import io
import time
from typing import Any

from PIL import Image
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
        tool_call("market", base="USD", quote="RUB"),
        "Rates are down.",
    )
    asked = await conversation.send(KIM, DORM, "derp usd to rub")
    await conversation.settle(asked)
    assert results_of(model, "market") == [
        "Market data is unavailable right now (HTTP 503); say so or try another way."
    ]


def bitcoin(web: dict[str, Any]) -> None:
    """Binance's BTC/USDT over the past week, sliding from 80,000 to 76,881."""
    now = time.time()
    web["/api/v3/ticker/24hr"] = {"lastPrice": "76881", "openPrice": "77500", "quoteVolume": "1800000000"}
    web["/api/v3/klines"] = [
        [(now - (168 - i) * 3600) * 1000, 0, 0, 0, str(80000 - i * 18.5)] for i in range(168)
    ]
    web["/fapi/v1/premiumIndex"] = {"lastFundingRate": "0.0001", "markPrice": "76900"}
    web["/fapi/v1/openInterest"] = {"openInterest": "93700"}


async def test_a_price_comes_with_its_chart(
    conversation: Conversation, model: ScriptedModel, web: dict[str, Any]
) -> None:
    bitcoin(web)
    conversation.meet(KIM, DORM)
    model.reply(tool_call("market", base="BTC", period="1w", chart=True), ".")
    asked = await conversation.send(KIM, DORM, "derp how's btc doing this week? chart pls")
    chart = await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert Image.open(io.BytesIO(chart.attachments[0].data)).size == (1200, 675)
    [result] = results_of(model, "market")
    assert result.startswith("BTC (BTC/USDT, Binance): 76,881 USDT. Past week: -3.9%, range 76,881–80,000.")
    assert "perpetual funding +0.0100% per 8h" in result and "\nSent it as #" in result
    later = conversation.api.calls_of("sendRichMessage", after=chart.sequence)
    assert later == []  # the chart is the answer (a group's hello comes before it)


async def test_a_chart_can_become_a_meme(
    conversation: Conversation, model: ScriptedModel, web: dict[str, Any]
) -> None:
    bitcoin(web)
    conversation.meet(KIM, DORM)
    model.reply(
        tool_call("market", base="BTC", period="1w", chart=True, final=False),
        tool_call("make_meme", text="HODL", source="r1"),
        ".",
    )
    asked = await conversation.send(KIM, DORM, "derp meme the btc chart")
    meme = await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert conversation.api.calls_of("sendPhoto", after=asked.after) == [
        meme
    ]  # the chart itself stays unposted
    assert 'Kept it as "r1"' in results_of(model, "market")[0]
    assert results_of(model, "make_meme")[0].startswith("Sent it as #")


async def test_the_most_bet_on_event_comes_with_its_odds_chart(
    conversation: Conversation, model: ScriptedModel, web: dict[str, Any]
) -> None:
    def outcome(title: str, chance: float) -> dict[str, Any]:
        return {
            "groupItemTitle": title, "active": True, "closed": False, "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{chance}", "{1 - chance:.2f}"]', "clobTokenIds": f'["{title}", "no"]',
        }  # fmt: skip

    web["/events"] = [
        {
            "title": "UEFA Champions League: 2027 Champion", "slug": "ucl-2027", "volume": 68_600_000,
            "volume24hr": 1_073_366, "endDate": "2027-06-20T00:00:00Z",
            "markets": [outcome("Barcelona", 0.24), outcome("Arsenal", 0.14), outcome("Como", 0.02)],
        }
    ]  # fmt: skip
    now = time.time()
    web["/prices-history"] = {"history": [{"t": now - 86400 * i, "p": 0.2} for i in range(30, 0, -1)]}
    conversation.meet(KIM, DORM)
    model.reply(tool_call("market", base="popular", kind="prediction", period="1m", chart=True), ".")
    asked = await conversation.send(KIM, DORM, "derp what are people betting on? show me")
    chart = await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert Image.open(io.BytesIO(chart.attachments[0].data)).size == (1200, 675)
    [result] = results_of(model, "market")
    assert result.startswith(
        "UEFA Champions League: 2027 Champion (Polymarket). The most bet-on events in the last day: UEFA "
        "Champions League: 2027 Champion: Barcelona 24%; Arsenal 14%; Como 2% (24h volume 1.1M USD). Barcelona: "
        "+4.0 pts over the past month, range 20–24%. The first one: volume 68.6M USD, resolves by 2027-06-20. "
        "https://polymarket.com/event/ucl-2027"
    )


async def test_keyed_lookups_say_when_they_are_not_set_up(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(KIM, DORM)
    model.reply(tool_call("lookup", source="wolfram", query="integrate x^2"), "None here.")
    asked = await conversation.send(KIM, DORM, "derp ask wolfram")
    await conversation.settle(asked)
    found = {tool.name for tool in model.infos[-1].function_tools}
    assert "lookup" in found and "run_program" not in found  # the program runner needs its key to appear
    assert results_of(model, "lookup") == ["WolframAlpha isn't set up here; use run_code or the web."]
