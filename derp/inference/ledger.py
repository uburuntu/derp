"""Write provider calls to the ledger (``provider_calls``)."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.models import ProviderCall
from derp.inference.openrouter import CallUsage


def record_usage(
    session: AsyncSession,
    usage: CallUsage,
    *,
    kind: str,
    funding: str,
    role: str | None = None,
    run_id: int | None = None,
    chat_id: int | None = None,
    user_id: int | None = None,
    requested_model: str | None = None,
    units: int = 0,
) -> ProviderCall:
    call = ProviderCall(
        kind=kind,
        role=role,
        model=usage.model or requested_model or "unknown",
        downstream_provider=usage.downstream_provider,
        generation_id=usage.generation_id,
        run_id=run_id,
        chat_id=chat_id,
        user_id=user_id,
        funding=funding,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        web_search_requests=usage.web_search_requests,
        units=units,
        cost_usd=usage.cost_usd,
    )
    session.add(call)
    return call


def simple_usage(model: str, cost_usd: Decimal, **counts: int) -> CallUsage:
    """Usage for non-chat calls (images, STT) that only report a cost."""
    return CallUsage(
        cost_usd=cost_usd,
        model=model,
        downstream_provider=None,
        generation_id=None,
        input_tokens=counts.get("input_tokens", 0),
        output_tokens=counts.get("output_tokens", 0),
        cache_read_tokens=0,
        reasoning_tokens=0,
        web_search_requests=0,
    )


async def cancelled_cost(api: OpenRouterAPI, generation_id: str) -> Decimal | None:
    """A stopped stream never receives its final usage chunk; ask OpenRouter afterwards.

    Returns None when the generation is not (yet) available — lookups were observed
    to 404 for fresh ZDR generations — so callers fall back to a token-price estimate.
    """
    try:
        data = await api.generation(generation_id)
    except OpenRouterError as error:
        if error.status == 404:
            return None
        raise
    cost = data.get("total_cost")
    return Decimal(str(cost)) if cost is not None else None


def estimate_cost(
    input_tokens: int, output_tokens: int, prompt_per_m: float, completion_per_m: float
) -> Decimal:
    """Price-table estimate, used only when OpenRouter cannot report a cost."""
    usd = input_tokens * prompt_per_m / 1e6 + output_tokens * completion_per_m / 1e6
    return Decimal(str(round(usd, 8)))
