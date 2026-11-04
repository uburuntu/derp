"""Which contributed key serves a turn, what it has left this cycle, and the pin it runs on.

A key serves a chat once it is on there (``key_chats.state = 'on'``: an admin turned it on in a group;
its owner's own private chat has it on from the start), in the order keys were turned on. A key is skipped while paused (its cap reached
or its provider's credits gone, until the next cycle; or paused by its owner), once it stopped working, and
when what is left of its cap can't cover a typical turn on its model. Spend is what the provider charged
(``provider_calls.upstream_usd``) since the cycle began; the cycle restarts monthly on the day the key was
added.
"""

import calendar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.config import KeyModel, Settings
from derp.inference.ledger import estimate_cost
from derp.inference.models import ProviderCall
from derp.inference.roles import RolePin
from derp.keys.models import ContributedKey, KeyChat, KeyModelCheck
from derp.keys.vault import unseal


@dataclass(frozen=True, slots=True)
class Prices:
    """USD per 1M tokens on a key's model: the scout's latest, else the configured list price."""

    prompt: float
    completion: float
    cache_read: float | None = None

    def turn(self, input_tokens: int, output_tokens: int) -> Decimal:
        """A turn priced uncached: what a key must still cover to be worth starting on."""
        return estimate_cost(input_tokens, output_tokens, self.prompt, self.completion)


@dataclass(frozen=True, slots=True)
class KeyRoute:
    """The contributed key paying a run's turns: its model, sub-key and what is left of this cycle's cap."""

    key_id: int
    user_id: int  # the contributor
    provider: str
    pin: RolePin
    prices: Prices
    left_usd: Decimal  # cap minus this cycle's spend, when the run started
    sub_key: str = field(repr=False)


def curated(settings: Settings, provider: str) -> tuple[KeyModel, ...]:
    return settings.keys.models.get(provider, ())


def curated_model(settings: Settings, model: str) -> KeyModel | None:
    return next((m for ms in settings.keys.models.values() for m in ms if m.model == model), None)


def key_pin(provider: str, model: str) -> RolePin:
    """The pin a key's turns run on: its model alone, on its provider's own endpoints, never ZDR.

    No fallbacks: the key serves exactly the model its owner picked, and shared capacity is removed for it
    (``OpenRouterManagement.register``), so a failure is the key's to report, never someone else's spend.
    """
    return RolePin(model=model, zdr=False, only={model: (provider,)})


async def prices(session: AsyncSession, settings: Settings, model: str) -> Prices | None:
    """The model's price: the scout's latest check, else its list price; None for a model no longer curated."""
    listed = curated_model(settings, model)
    if listed is None:
        return None
    check = await session.get(KeyModelCheck, model)
    if check is not None and check.price_prompt is not None:
        return Prices(
            float(check.price_prompt),
            float(check.price_completion or 0),
            float(check.price_cache_read) if check.price_cache_read is not None else None,
        )
    return Prices(listed.prompt, listed.completion, listed.cache_read)


async def offered_models(session: AsyncSession, settings: Settings, provider: str) -> list[KeyModel]:
    """The curated models a key on ``provider`` may pick: all but those the scout last found dead."""
    models = curated(settings, provider)
    dead = set(
        await session.scalars(
            select(KeyModelCheck.model).where(
                KeyModelCheck.model.in_([m.model for m in models]), KeyModelCheck.live.is_(False)
            )
        )
    )
    return [m for m in models if m.model not in dead]


async def serving_model(session: AsyncSession, settings: Settings, key: ContributedKey) -> str | None:
    """The model a key runs: its owner's pick, or while that one isn't offered (the scout found it dead, or it
    left the list), the provider's first model still offered. Keys stay registered for the whole list
    (``catalog.check_key_models`` re-syncs them daily), so the stand-in runs on the key too."""
    offered = [m.model for m in await offered_models(session, settings, key.provider)]
    if key.model in offered:
        return key.model
    return offered[0] if offered else None


def cycle_start(anchor: date, now: datetime) -> datetime:
    """The start of the current monthly cycle: the anchor's day of this month (or the last one, when it hasn't
    come yet), clamped to short months."""

    def on(year: int, month: int) -> date:
        return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))

    today = now.astimezone(UTC).date()
    start = on(today.year, today.month)
    if start > today:
        year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        start = on(year, month)
    return datetime.combine(start, time(), tzinfo=UTC)


def next_cycle(anchor: date, now: datetime) -> datetime:
    start = cycle_start(anchor, now).date()
    year, month = (start.year, start.month + 1) if start.month < 12 else (start.year + 1, 1)
    return datetime.combine(
        date(year, month, min(anchor.day, calendar.monthrange(year, month)[1])), time(), tzinfo=UTC
    )


async def spent(
    session: AsyncSession, key: ContributedKey, now: datetime, chat_id: int | None = None
) -> Decimal:
    """What the provider charged for this key since its cycle began (in one chat, when given)."""
    where = [ProviderCall.key_id == key.id, ProviderCall.created_at >= cycle_start(key.cycle_anchor, now)]
    if chat_id is not None:
        where.append(ProviderCall.chat_id == chat_id)
    total = await session.scalar(select(func.coalesce(func.sum(ProviderCall.upstream_usd), 0)).where(*where))
    return Decimal(total or 0)


def serving(now: datetime) -> list[ColumnElement[bool]]:
    """Keys able to serve now: registered, working, and not paused (a cycle pause lapses by itself)."""
    return [
        ContributedKey.removed_at.is_(None),
        ContributedKey.dead_at.is_(None),
        or_(
            ContributedKey.paused.is_(None),
            (ContributedKey.paused != "owner") & (ContributedKey.paused_until <= now),
        ),
    ]


async def candidates(session: AsyncSession, chat_id: int, now: datetime) -> list[ContributedKey]:
    """Keys that may serve this chat, in serving order."""
    statement = (
        select(ContributedKey)
        .join(KeyChat, KeyChat.key_id == ContributedKey.id)
        .where(KeyChat.chat_id == chat_id, KeyChat.state == "on", *serving(now))
        .order_by(KeyChat.enabled_at, ContributedKey.id)
    )
    return list(await session.scalars(statement))


async def choose(
    session: AsyncSession,
    settings: Settings,
    *,
    chat_id: int,
    turn_tokens: tuple[int, int],
    now: datetime | None = None,
) -> KeyRoute | None:
    """The first key that can cover a typical turn (``turn_tokens``: input, output) on its model, or None.

    A key whose cap is used up is paused until its next cycle here, so it isn't priced on every ask.
    """
    moment = now or datetime.now(UTC)
    for key in await candidates(session, chat_id, moment):
        model = await serving_model(session, settings, key)
        priced = await prices(session, settings, model) if model else None
        if model is None or priced is None:  # nothing it may run is offered now: the owner picks again
            continue
        left = key.cap_usd - await spent(session, key, moment)
        if left < priced.turn(*turn_tokens):
            await pause(session, key, "cap", next_cycle(key.cycle_anchor, moment))
            continue
        # Runs on this key still in flight haven't ledgered their turns: each is set aside at a typical turn,
        # so keys shared by busy chats overshoot their cap by little.
        flying = await session.scalar(
            select(func.count()).where(AgentRun.key_id == key.id, AgentRun.status == "running")
        )
        left -= priced.turn(*turn_tokens) * (flying or 0)
        if key.paused is not None:  # a cycle pause that lapsed
            await resume(session, key)
        return KeyRoute(
            key_id=key.id,
            user_id=key.user_id,
            provider=key.provider,
            pin=key_pin(key.provider, model),
            prices=priced,
            left_usd=left,
            sub_key=unseal(settings.keys_secret, key.sub_key),
        )
    return None


async def pause(session: AsyncSession, key: ContributedKey, why: str, until: datetime | None) -> bool:
    """Pause a key (``why``: cap / exhausted / owner); False when it already was for that reason.

    Its chats and owner hear about it once (``keys.notices.announce``).
    """
    if key.paused == why and key.paused_until == until:
        return False
    key.paused, key.paused_until, key.noticed_at = why, until, None
    await session.execute(update(KeyChat).where(KeyChat.key_id == key.id).values(paused_noticed_at=None))
    return True


async def resume(session: AsyncSession, key: ContributedKey) -> None:
    key.paused, key.paused_until, key.noticed_at = None, None, None
    await session.execute(update(KeyChat).where(KeyChat.key_id == key.id).values(paused_noticed_at=None))
