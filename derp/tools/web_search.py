"""Web search tool wrapper with credit awareness.

Wraps the DuckDuckGo search tool with credit/limit checking.
DuckDuckGo is free, but we still track daily usage for limits.
"""

from __future__ import annotations

import asyncio

import logfire
from ddgs import DDGS
from pydantic_ai import RunContext

from derp.llm.deps import AgentDeps
from derp.tools.wrapper import credit_aware_tool


def _search(query: str, max_results: int) -> list[dict[str, str]]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


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

    results = await asyncio.to_thread(_search, query, max_results)
    if not results:
        return f"No results found for: {query}"

    formatted = []
    for index, result in enumerate(results, 1):
        title = result.get("title", "No title")
        body = result.get("body", "No description")
        url = result.get("href", "")
        formatted.append(f"{index}. **{title}**\n   {body}\n   {url}")

    return "\n\n".join(formatted)
