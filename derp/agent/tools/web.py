"""Web search and page fetch: OpenRouter server tools, run on OpenRouter's side.

Search costs a per-call fee that OpenRouter includes in the response's cost, so
it is metered like the rest of the answer; free runs get neither tool (the fee
would land on the creator: ``TIERS``). Searches run inside one model request,
past the per-tool-call money check, so each request allows only the searches the run's
hold still covers (``Settings.web_search_price_usd`` each, after one more turn
like the latest: ``outputs.hold_left``); OpenRouter answers a search past
``max_uses`` with a "limit reached" result. When none fits, search is left out
of the request and the guard tells the model it is out of budget for this
answer (``search_budget`` is 0). Fetching uses OpenRouter's own engine; Derp's
host never downloads pages. Search backends are not covered by zero data retention.
"""

from pydantic_ai import RunContext
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool

from derp.agent.deps import RunDeps
from derp.agent.outputs import hold_left
from derp.money.funding import Funding

TIERS = frozenset({Funding.PAID, Funding.SPONSORED})
SEARCHES = 2  # per model request
FETCHES = 3
FETCH_TOKENS = 6000  # page text kept per fetch


async def search_budget(ctx: RunContext[RunDeps]) -> int | None:
    """Searches the coming request may make: what the hold covers after one more turn; None on free runs."""
    deps = ctx.deps
    if deps.funding not in TIERS:
        return None
    left = await hold_left(ctx, next_turn=True)
    if left is None:
        return SEARCHES
    fee = deps.settings.credits_for_usd(deps.settings.web_search_price_usd)
    return max(0, min(SEARCHES, left // max(fee, 1)))


async def web_search(ctx: RunContext[RunDeps]) -> WebSearchTool | None:
    searches = await search_budget(ctx)
    return WebSearchTool(search_context_size="low", max_uses=searches) if searches else None


def web_fetch(ctx: RunContext[RunDeps]) -> WebFetchTool | None:
    if ctx.deps.funding not in TIERS:
        return None
    return WebFetchTool(max_uses=FETCHES, max_content_tokens=FETCH_TOKENS)


WEB_CAPABILITIES: list[NativeTool[RunDeps]] = [NativeTool(web_search), NativeTool(web_fetch)]
