"""The scout's daily look at the curated key models (``settings.keys.models``).

A model stays offered while its provider's own endpoint is listed with tool calling (the only route a key
can take) and, at most weekly, passes the same tiny live check challengers do (on Derp's credits, through
that endpoint: a cent or so). Its price is taken from that endpoint, for the picker and turn estimates.
A model that drops out disappears from the picker, and keys picked on it serve on their provider's
recommended model until their owner picks another (``routing.serving_model``); the change lands in the
scout's note. Every live key is then re-registered for today's list, which a deploy may have changed.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from derp.agent.runs import ledger_side_call
from derp.config import Settings
from derp.db.engine import Database
from derp.inference.api import OpenRouterAPI, OpenRouterError, OpenRouterManagement
from derp.inference.openrouter import ModelFactory
from derp.inference.roles import ModelRole
from derp.inference.scout import MIN_UPTIME, SMOKE_TOKENS, passes_smoke, per_million
from derp.keys.models import ContributedKey, KeyModelCheck
from derp.keys.register import allow_models
from derp.keys.routing import key_pin

log = logging.getLogger("derp.keys")
SMOKE_EVERY = timedelta(days=7)


def _own_route(endpoints: list[dict[str, Any]], provider: str) -> dict[str, Any] | None:
    """The provider's own endpoint with tool calling, if listed and up."""
    for endpoint in endpoints:
        tag = str(endpoint.get("tag") or "")
        uptime = endpoint.get("uptime_last_1d")
        if (
            tag.split("/")[0] == provider
            and "tools" in (endpoint.get("supported_parameters") or [])
            and (uptime is None or uptime >= MIN_UPTIME)
        ):
            return endpoint
    return None


async def check_key_models(
    db: Database,
    api: OpenRouterAPI,
    settings: Settings,
    model_factory: ModelFactory,
    management: OpenRouterManagement | None = None,
) -> list[str]:
    """Refresh every curated model's liveness and price, and keep every key registered for today's list;
    returns notes on models that changed state."""
    now = datetime.now(UTC)
    notes: list[str] = []
    for provider, models in settings.keys.models.items():
        for listed in models:
            try:
                route = _own_route(await api.model_endpoints(listed.model), provider)
            except OpenRouterError as error:
                log.info("key model lookup failed: %s", error.status)
                continue
            async with db.session() as session:
                check = await session.get(KeyModelCheck, listed.model)
                was_live = check.live if check is not None else True
                smoked = check.smoked_at if check is not None else None
            live = route is not None
            if live and (smoked is None or now - smoked > SMOKE_EVERY):
                pin = key_pin(provider, listed.model)
                ok, paid = await passes_smoke(model_factory(pin), pin)
                await ledger_side_call(
                    db, paid, kind="scout", funding="creator", role=ModelRole.CHAT, context_tokens=SMOKE_TOKENS,
                    requested_model=listed.model,
                )  # fmt: skip
                live, smoked = ok, now if ok else smoked
            pricing = (route or {}).get("pricing") or {}
            async with db.transaction() as session:
                row = await session.get(KeyModelCheck, listed.model) or KeyModelCheck(model=listed.model)
                session.add(row)
                row.live, row.checked_at, row.smoked_at = live, now, smoked
                if pricing.get("prompt") is not None and pricing.get("completion") is not None:
                    row.price_prompt = per_million(pricing["prompt"])
                    row.price_completion = per_million(pricing["completion"])
                    cache = pricing.get("input_cache_read")
                    row.price_cache_read = per_million(cache) if cache is not None else None
            if live != was_live:
                notes.append(f"key model {listed.model}: {'back' if live else 'no longer offered'}")
    if management is not None:
        await sync_registrations(db, settings, management)
    return notes


async def sync_registrations(db: Database, settings: Settings, management: OpenRouterManagement) -> None:
    """Keep every key registered for today's models. Runs at startup, before polling: a model a deploy added
    that a key isn't registered for would be served on OpenRouter's shared capacity, on Derp's credits."""
    async with db.session() as session:
        keys = list(await session.scalars(select(ContributedKey).where(ContributedKey.removed_at.is_(None))))
    for key in keys:
        try:
            await allow_models(settings, management, key)
        except OpenRouterError as error:
            log.info("re-registering a key's models failed: %s", error.status)
