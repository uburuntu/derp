"""Web search and page fetch: OpenRouter server tools, run on OpenRouter's side.

Search costs a per-call fee that OpenRouter includes in the response's cost, so
it is metered like the rest of the answer; free runs get neither tool (the fee
would land on the creator). Fetching uses OpenRouter's own engine; Derp's host
never downloads pages. Search backends are not covered by zero data retention.
"""

from pydantic_ai import RunContext
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool

from derp.agent.deps import RunDeps
from derp.money.funding import Funding

SEARCHES = 2  # per model request
FETCHES = 3
FETCH_TOKENS = 6000  # page text kept per fetch


def web_search(ctx: RunContext[RunDeps]) -> WebSearchTool | None:
    if ctx.deps.funding is Funding.FREE:
        return None
    return WebSearchTool(search_context_size="low", max_uses=SEARCHES)


def web_fetch(ctx: RunContext[RunDeps]) -> WebFetchTool | None:
    if ctx.deps.funding is Funding.FREE:
        return None
    return WebFetchTool(max_uses=FETCHES, max_content_tokens=FETCH_TOKENS)


WEB_CAPABILITIES: list[NativeTool[RunDeps]] = [NativeTool(web_search), NativeTool(web_fetch)]
