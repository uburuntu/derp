"""Web search tool wrapper with credit awareness.

Wraps the DuckDuckGo search tool with credit/limit checking.
DuckDuckGo is free, but we still track daily usage for limits.
"""

from __future__ import annotations

import logfire
from pydantic_ai import RunContext
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool

from derp.llm.deps import AgentDeps
from derp.tools.wrapper import credit_aware_tool

# Get the underlying DuckDuckGo search function
_ddg_search = duckduckgo_search_tool()


@credit_aware_tool("web_search")
async def web_search(
    ctx: RunContext[AgentDeps],
    query: str,
    *,
    max_results: int = 5,
) -> str:
    """Search the web for current information.

    Use this tool when you need up-to-date information, facts, or data
    that might not be in your training data. Good for news, prices,
    weather, or verifying current facts.

    Args:
        ctx: The run context with agent dependencies.
        query: The search query.
        max_results: Maximum number of results to return (default 5).

    Returns:
        Search results as formatted text.
    """
    logfire.info(
        "web_search_requested",
        query_length=len(query),
        max_results=max_results,
        chat_id=ctx.deps.chat_id,
    )

    # The DDG tool doesn't need our context, it's standalone
    # We need to call the underlying function directly
    # Note: The pydantic-ai DDG tool is a simple function, not agent-aware

    try:
        # DuckDuckGo search is synchronous, but wrapped for async
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return f"No results found for: {query}"

        # Format results
        formatted = []
        for i, result in enumerate(results, 1):
            title = result.get("title", "No title")
            body = result.get("body", "No description")
            url = result.get("href", "")
            formatted.append(f"{i}. **{title}**\n   {body}\n   {url}")

        return "\n\n".join(formatted)

    except Exception as e:
        logfire.exception("web_search_failed", query=query)
        return f"Search failed: {e}"
