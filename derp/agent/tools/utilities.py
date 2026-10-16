"""Utility tools: time zones, weather, rates and reference lookups. Free on every tier.

All are deferred (found through tool search). Arithmetic lives in the Python
sandbox (``derp.agent.tools.code``). WolframAlpha and the remote program runner
appear only when their keys are configured.
"""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.toolsets import Family
from derp.tools import lookups
from derp.tools.timezones import TimeZoneError, convert_time

toolset = FunctionToolset[RunDeps]()
wolfram_tools = FunctionToolset[RunDeps]()
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
async def wiki(ctx: RunContext[RunDeps], query: str) -> str:
    """The Wikipedia summary of the best-matching article (Russian Wikipedia for Cyrillic queries).

    Args:
        query: What to look up.
    """
    try:
        return await ctx.deps.lookups.wiki(query)
    except lookups.LookupError as error:
        return _failed("Wikipedia", error)


@toolset.tool
async def urban_dictionary(ctx: RunContext[RunDeps], term: str) -> str:
    """Slang definitions from Urban Dictionary (crowd-written, often crude).

    Args:
        term: The slang word or phrase.
    """
    try:
        return await ctx.deps.lookups.urban(term)
    except lookups.LookupError as error:
        return _failed("Urban Dictionary", error)


@toolset.tool
async def arxiv_search(ctx: RunContext[RunDeps], query: str) -> str:
    """Search arXiv papers: titles, authors, year, link and the start of the abstract.

    Args:
        query: Topic or title words.
    """
    try:
        return await ctx.deps.lookups.arxiv(query)
    except lookups.LookupError as error:
        return _failed("arXiv", error)


@wolfram_tools.tool
async def wolfram(ctx: RunContext[RunDeps], query: str) -> str:
    """WolframAlpha short answer: math, units, science, dates, facts with computation.

    Args:
        query: A question in English, like `distance from Moscow to Paris` or `integrate x^2 sin x`.
    """
    try:
        return await ctx.deps.lookups.wolfram(query)
    except lookups.LookupError as error:
        return _failed("WolframAlpha", error)


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


family = Family("utilities", toolset, deferred=True)
wolfram_family = Family(
    "wolfram", wolfram_tools, deferred=True, available=lambda deps: bool(deps.settings.wolfram_app_id)
)
code_family = Family(
    "code", code_tools, deferred=True, available=lambda deps: bool(deps.settings.jdoodle_client_secret)
)
