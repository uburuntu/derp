"""Web search and page fetch: OpenRouter server tools, run on OpenRouter's side.

Search costs a per-call fee that OpenRouter includes in the response's cost, so
it is metered like the rest of the answer; free runs get neither tool (the fee
would land on the creator). Searches run inside one model request, past the
per-tool-call money check, so each request allows only the searches the run's
hold still covers (``Settings.web_search_price_usd`` each, after one more turn
like the latest); OpenRouter answers a search past ``max_uses`` with a "limit
reached" result. When none fits, search is left out of the request and the
model is told it is out of budget for this answer (``search_withheld``; the
guard adds the request-only note). Fetching uses OpenRouter's own engine; Derp's host never downloads
pages. Search backends are not covered by zero data retention.
"""

from pydantic_ai import RunContext
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.messages import ModelResponse
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool

from derp.agent.deps import RunDeps
from derp.agent.runs import totals
from derp.agent.tools.images import spent_credits
from derp.money.funding import Funding

SEARCHES = 2  # per model request
FETCHES = 3
FETCH_TOKENS = 6000  # page text kept per fetch


async def affordable_searches(ctx: RunContext[RunDeps]) -> int:
    """Searches the run's remaining hold covers, after one more request like the latest (at least a credit)."""
    deps = ctx.deps
    if deps.funds.reservation_id is None:
        return SEARCHES
    run = ctx.messages[deps.history_start :]
    latest = next((m for m in reversed(run) if isinstance(m, ModelResponse)), None)
    turn = deps.settings.credits_for_usd(float(totals([latest]).cost_usd)) if latest is not None else 0
    left = deps.funds.credits - await spent_credits(ctx) - max(turn, 1)
    fee = deps.settings.credits_for_usd(deps.settings.web_search_price_usd)
    return max(0, min(SEARCHES, left // max(fee, 1)))


async def web_search(ctx: RunContext[RunDeps]) -> WebSearchTool | None:
    if ctx.deps.funding is Funding.FREE:
        return None
    searches = await affordable_searches(ctx)
    ctx.deps.search_withheld = searches == 0
    if searches == 0:
        return None
    return WebSearchTool(search_context_size="low", max_uses=searches)


def web_fetch(ctx: RunContext[RunDeps]) -> WebFetchTool | None:
    if ctx.deps.funding is Funding.FREE:
        return None
    return WebFetchTool(max_uses=FETCHES, max_content_tokens=FETCH_TOKENS)


WEB_CAPABILITIES: list[NativeTool[RunDeps]] = [NativeTool(web_search), NativeTool(web_fetch)]
