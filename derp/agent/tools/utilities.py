"""Utility tools: time zones, weather, rates and reference lookups. Free on every tier.

All are deferred (found through tool search on free runs). Arithmetic lives in the
Python sandbox (``derp.agent.tools.code``). ``lookup`` asks one of several reference
sources; WolframAlpha answers only when its key is configured, and the remote program
runner appears only with its key.
"""

from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.toolsets import Family
from derp.tools import lookups
from derp.tools.timezones import TimeZoneError, convert_time

toolset = FunctionToolset[RunDeps]()
code_tools = FunctionToolset[RunDeps]()


def _failed(name: str, error: lookups.LookupError) -> str:
    return f"{name} is unavailable right now ({error.reason}); say so or try another way."


@toolset.tool_plain
def time_convert(source_zone: str, target_zones: list[str], time: str | None = None) -> str:
    """Convert a time between IANA time zones (now, if no time is given).

    Args:
        source_zone: Like Europe/Moscow.
        target_zones: Like ["Asia/Tokyo", "America/New_York"].
        time: ISO local time in the source zone, like 2026-10-20 15:30.
    """
    try:
        return convert_time(time, source_zone, target_zones)
    except TimeZoneError as error:
        return str(error)


@toolset.tool
async def weather(ctx: RunContext[RunDeps], place: str) -> str:
    """Current weather and a 3-day forecast for a place.

    Args:
        place: City or place name.
    """
    try:
        return await ctx.deps.lookups.weather(place)
    except lookups.LookupError as error:
        return _failed("Weather", error)


@toolset.tool
async def exchange_rate(ctx: RunContext[RunDeps], base: str, quote: str, amount: float = 1) -> str:
    """Convert money: fiat currencies (ISO codes like USD, RUB) and major crypto (BTC, ETH, TON…).

    Args:
        base: Currency to convert from.
        quote: Currency to convert to.
        amount: How much of the base currency.
    """
    try:
        return await ctx.deps.lookups.exchange_rate(base, quote, amount)
    except lookups.LookupError as error:
        return _failed("Exchange rates", error)


@toolset.tool
async def lookup(
    ctx: RunContext[RunDeps], source: Literal["wikipedia", "urban_dictionary", "arxiv", "wolfram"], query: str
) -> str:
    """Look something up: wikipedia (the best article's summary; Russian Wikipedia for Cyrillic), urban_dictionary
    (slang, crowd-written and crude), arxiv (papers: authors, year, link, the abstract's start) or wolfram
    (WolframAlpha's short answer: maths, units, science, dates; ask in English).

    Args:
        source: Where to look.
        query: What to look up, e.g. "integrate x^2 sin x" for wolfram.
    """
    client = ctx.deps.lookups
    match source:
        case "wikipedia":
            name, call = "Wikipedia", client.wiki
        case "urban_dictionary":
            name, call = "Urban Dictionary", client.urban
        case "arxiv":
            name, call = "arXiv", client.arxiv
        case "wolfram":
            if not ctx.deps.settings.wolfram_app_id:
                return "WolframAlpha isn't set up here; use run_code or the web."
            name, call = "WolframAlpha", client.wolfram
    try:
        return await call(query)
    except lookups.LookupError as error:
        return _failed(name, error)


@code_tools.tool
async def run_program(ctx: RunContext[RunDeps], language: str, source: str, stdin: str = "") -> str:
    """Run a short program in a language other than Python in a remote sandbox (Python: use run_code).

    Args:
        language: JDoodle language code: nodejs, cpp17, java, go, rust, bash, sql…
        source: The complete program.
        stdin: Input for the program.
    """
    try:
        return await ctx.deps.lookups.run_program(language, source, stdin)
    except lookups.LookupError as error:
        return _failed("The code runner", error)


family = Family(
    "utilities",
    toolset,
    deferred=True,
    detached="lookups (weather, exchange rates, time zones, Wikipedia, WolframAlpha and the like)",
)
code_family = Family(
    "code",
    code_tools,
    deferred=True,
    available=lambda deps: bool(deps.settings.jdoodle_client_secret),
    detached="a runner for programs in other languages",
)
