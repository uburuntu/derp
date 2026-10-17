"""Daily model scout: keeps role pins alive and cheap, proposes better models.

Safe changes apply automatically: a dead or delisted primary is replaced by
its first live fallback, and the price snapshot follows the cheapest live ZDR
route. Anything else (a smarter or cheaper challenger) becomes an owner
proposal; challengers must first pass a tiny live check (answers Russian in
Russian, calls a tool when asked).
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.runs import ledger_side_call
from derp.config import Settings
from derp.db.engine import Database
from derp.inference.api import OpenRouterAPI
from derp.inference.models import ModelPin, ProviderCall, ScoutProposal
from derp.inference.openrouter import ModelFactory, chat_settings, responses
from derp.inference.pins import PinStore, apply_pin
from derp.inference.roles import ModelRole, RolePin

log = logging.getLogger("derp.scout")
MIN_UPTIME = 95.0
CHAT_ROLES = (ModelRole.CHAT, ModelRole.CHAT_SPONSORED)
OUTPUT_TOKENS_PER_TURN = 400
INTELLIGENCE_MARGIN = 2.0


@dataclass(frozen=True, slots=True)
class Route:
    tag: str
    prompt: Decimal  # USD per 1M tokens
    completion: Decimal
    uptime: float | None
    tools: bool
    cache_read: Decimal | None = None  # USD per 1M cached input tokens; None: no discount known

    @property
    def live(self) -> bool:
        return self.uptime is None or self.uptime >= MIN_UPTIME


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    intelligence: float | None
    tools: bool
    image_input: bool
    prompt: Decimal
    completion: Decimal


@dataclass(slots=True)
class Catalog:
    models: dict[str, Candidate] = field(default_factory=dict[str, Candidate])
    zdr: dict[str, list[Route]] = field(default_factory=dict[str, list[Route]])
    images: set[str] = field(default_factory=set[str])

    def live_routes(self, model: str, *, tools: bool = True) -> list[Route]:
        return [r for r in self.zdr.get(model, []) if r.live and (r.tools or not tools)]

    def cheapest(self, model: str, *, tools: bool = True) -> Route | None:
        routes = self.live_routes(model, tools=tools)
        return min(routes, key=lambda r: (r.prompt, r.completion)) if routes else None


@dataclass(frozen=True, slots=True)
class Change:
    role: ModelRole
    pin: RolePin
    reason: str
    safe: bool
    price: tuple[Decimal, Decimal] | None = None


def per_million(value: object) -> Decimal:
    return Decimal(str(value or 0)) * 1_000_000


def build_catalog(
    models: Iterable[Mapping[str, Any]], zdr: Iterable[Mapping[str, Any]], images: Iterable[str]
) -> Catalog:
    catalog = Catalog(images=set(images))
    for model in models:
        pricing = model.get("pricing") or {}
        if pricing.get("prompt") is None or pricing.get("completion") is None:
            continue  # unknown price is not "free"
        analysis = (model.get("benchmarks") or {}).get("artificial_analysis") or {}
        catalog.models[str(model["id"])] = Candidate(
            id=str(model["id"]),
            intelligence=analysis.get("intelligence_index"),
            tools="tools" in (model.get("supported_parameters") or []),
            image_input="image" in ((model.get("architecture") or {}).get("input_modalities") or []),
            prompt=per_million(pricing.get("prompt")),
            completion=per_million(pricing.get("completion")),
        )
    for endpoint in zdr:
        pricing = endpoint.get("pricing") or {}
        if pricing.get("prompt") is None or pricing.get("completion") is None:
            continue
        catalog.zdr.setdefault(str(endpoint["model_id"]), []).append(
            Route(
                tag=str(endpoint.get("tag") or ""),
                prompt=per_million(pricing.get("prompt")),
                completion=per_million(pricing.get("completion")),
                uptime=endpoint.get("uptime_last_1d"),
                tools="tools" in (endpoint.get("supported_parameters") or []),
                cache_read=(
                    per_million(pricing["input_cache_read"])
                    if pricing.get("input_cache_read") is not None
                    else None
                ),
            )
        )
    return catalog


def available(role: ModelRole, model: str, catalog: Catalog) -> bool:
    if role is ModelRole.IMAGE:
        return model in catalog.images
    if role is ModelRole.STT:
        return bool(catalog.live_routes(model, tools=False))
    if role is ModelRole.CHAT_FREE:
        candidate = catalog.models.get(model)
        return candidate is not None and candidate.tools
    return bool(catalog.live_routes(model))


def turn_cost(route: Route, window: int, cached: float = 0.0) -> Decimal:
    """A typical turn on this route: ``cached`` of the window is read from the prompt cache."""
    share = Decimal(str(min(max(cached, 0.0), 1.0)))
    cache_price = route.cache_read if route.cache_read is not None else route.prompt
    prompt = route.prompt * window * (1 - share) + cache_price * window * share
    return (prompt + route.completion * OUTPUT_TOKENS_PER_TURN) / 1_000_000


def _cheapest_turn(catalog: Catalog, model: str, window: int, cached: float) -> Route | None:
    routes = catalog.live_routes(model)
    return min(routes, key=lambda r: turn_cost(r, window, cached)) if routes else None


def plan(
    pins: Mapping[ModelRole, RolePin],
    catalog: Catalog,
    *,
    allowlist: tuple[str, ...],
    windows: Mapping[ModelRole, int],
    cached: float = 0.0,
) -> list[Change]:
    """``cached``: the measured share of chat input served from the prompt cache, for turn prices."""
    changes: list[Change] = []
    for role, pin in pins.items():
        current = pin
        if not available(role, pin.model, catalog):
            live = [m for m in pin.fallbacks if available(role, m, catalog)]
            if not live:
                log.warning("scout: no live model for role %s", role)
                continue
            rest = [m for m in pin.fallbacks if m != live[0]] + [pin.model]
            current = pin.model_copy(update={"model": live[0], "fallbacks": tuple(rest[:2])})
            changes.append(
                Change(role, current, f"{pin.model} is unavailable; promoted {live[0]}", safe=True)
            )
        if role in CHAT_ROLES or role is ModelRole.STT:
            route = catalog.cheapest(current.model, tools=role is not ModelRole.STT)
            if route is not None:
                changes.append(
                    Change(role, current, "price snapshot", safe=True, price=(route.prompt, route.completion))
                )
        if role in CHAT_ROLES and (
            challenger := _challenger(role, current, catalog, allowlist, windows[role], cached)
        ):
            changes.append(challenger)
    return changes


def _challenger(
    role: ModelRole, pin: RolePin, catalog: Catalog, allowlist: tuple[str, ...], window: int, cached: float
) -> Change | None:
    incumbent = catalog.models.get(pin.model)
    route = _cheapest_turn(catalog, pin.model, window, cached)
    if incumbent is None or route is None or incumbent.intelligence is None:
        return None
    budget = turn_cost(route, window, cached)
    best: tuple[float, Candidate, Route] | None = None
    for candidate in catalog.models.values():
        if (
            candidate.id == pin.model
            or candidate.id.endswith((":batch", ":free"))
            or not candidate.id.startswith(allowlist)
            or not (candidate.tools and candidate.image_input)
            or candidate.intelligence is None
            or candidate.intelligence < incumbent.intelligence + INTELLIGENCE_MARGIN
        ):
            continue
        cheapest = _cheapest_turn(catalog, candidate.id, window, cached)
        if cheapest is None or turn_cost(cheapest, window, cached) > budget:
            continue
        if best is None or candidate.intelligence > best[0]:
            best = (candidate.intelligence, candidate, cheapest)
    if best is None:
        return None
    score, candidate, cheapest = best
    proposed = pin.model_copy(update={"model": candidate.id, "fallbacks": (pin.model, *pin.fallbacks)[:2]})
    reason = (
        f"{candidate.id}: intelligence {score:g} vs {incumbent.intelligence:g}, "
        f"≈${turn_cost(cheapest, window, cached):.5f}/turn vs ${budget:.5f} ({cached:.0%} cached)"
    )
    return Change(role, proposed, reason, safe=False)


SMOKE_TOKENS = 100  # prompt tokens of one smoke check, to price a probe that came back without a cost


async def passes_smoke(model: Model, pin: RolePin) -> tuple[bool, list[ModelResponse]]:
    """Two tiny live checks under the pin's real settings (ZDR, fallbacks); returns the paid responses too.

    A challenger that fails either never reaches the owner.
    """
    called: list[str] = []
    agent = Agent(
        model, instructions="You are a helpful assistant.", model_settings=chat_settings(pin, max_tokens=300)
    )

    @agent.tool_plain
    def current_time() -> str:
        """Return the current time."""
        called.append("current_time")
        return "12:00"

    # One capture per run: a capture context only records the first run inside it.
    with capture_run_messages() as captured:
        try:
            russian = await agent.run("Ответь по-русски одним коротким предложением: какого цвета небо?")
        except Exception:  # noqa: BLE001 - any provider failure disqualifies the challenger
            return False, responses(captured)
    with capture_run_messages() as captured:
        try:
            timed = await agent.run("What time is it? Use the tool.")
        except Exception:  # noqa: BLE001
            return False, responses(russian.all_messages()) + responses(captured)
    paid = responses(russian.all_messages()) + responses(timed.new_messages())
    cyrillic = sum("а" <= ch.lower() <= "я" for ch in russian.output)
    return cyrillic >= 5 and bool(called) and bool(timed.output), paid


async def cached_share(session: AsyncSession, days: int = 7) -> float:
    """The share of paid and sponsored chat input served from the prompt cache lately (0 without data)."""
    since = datetime.now(UTC) - timedelta(days=days)
    row = (
        await session.execute(
            select(func.sum(ProviderCall.input_tokens), func.sum(ProviderCall.cache_read_tokens)).where(
                ProviderCall.kind == "chat",
                ProviderCall.role.in_([role.value for role in CHAT_ROLES]),
                ProviderCall.created_at >= since,
            )
        )
    ).one()
    total, cached = row
    return float(cached or 0) / float(total) if total else 0.0


async def run_scout(
    db: Database,
    api: OpenRouterAPI,
    store: PinStore,
    settings: Settings,
    model_factory: ModelFactory,
    notify: Callable[[str, list[int]], Awaitable[None]] | None = None,
) -> list[Change]:
    models = await api.models(supported_parameters="tools", sort="intelligence-high-to-low")
    zdr = await api.zdr_endpoints()
    images = [str(m["id"]) for m in await api.image_models()]
    catalog = build_catalog(models, zdr, images)  # the tools-capable list includes `:free` variants
    windows = {
        ModelRole.CHAT: settings.context_windows.paid,
        ModelRole.CHAT_SPONSORED: settings.context_windows.sponsored,
    }
    async with db.session() as session:
        cached = await cached_share(session)
    changes = plan(
        await store.all(), catalog, allowlist=settings.model_family_allowlist, windows=windows, cached=cached
    )

    # Live checks run outside any transaction: they take seconds and cost money.
    async with db.session() as session:
        pending = {
            str(model)
            for model in await session.scalars(
                select(ScoutProposal.proposed["model"].astext).where(ScoutProposal.status != "applied")
            )
        }
    passed: list[Change] = []
    for change in changes:
        if change.safe or change.pin.model in pending:
            continue
        ok, paid = await passes_smoke(model_factory(change.pin), change.pin)
        await ledger_side_call(
            db, paid, kind="scout", funding="creator", role=change.role, context_tokens=SMOKE_TOKENS,
            requested_model=change.pin.model,
        )  # fmt: skip
        if ok:
            passed.append(change)

    notes: list[str] = []
    proposed: list[int] = []
    async with db.transaction() as session:
        for change in changes:
            if not change.safe:
                continue
            await apply_pin(session, change.role, change.pin, source="scout")
            if change.price is not None and (row := await session.get(ModelPin, change.role.value)):
                row.price_prompt, row.price_completion = change.price
            if change.reason != "price snapshot":
                notes.append(f"{change.role}: {change.reason}")
        for change in passed:
            row = await session.get(ModelPin, change.role.value)
            current = row.pin if row is not None else (await store.get(change.role)).model_dump(mode="json")
            proposal = ScoutProposal(
                role=change.role.value,
                current=current,
                proposed=change.pin.model_dump(mode="json"),
                reason=change.reason,
            )
            session.add(proposal)
            await session.flush()
            proposed.append(proposal.id)
            notes.append(f"{change.role}: proposal #{proposal.id} — {change.reason}")
    await store.refresh()
    if notes and notify is not None:
        await notify("Model scout:\n" + "\n".join(notes), proposed)
    return changes
