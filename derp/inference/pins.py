"""Live model pins: DB-backed, seeded from settings, cached briefly in-process."""

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.inference.models import ModelPin
from derp.inference.roles import ModelRole, RolePin

log = logging.getLogger("derp.pins")
CACHE_SECONDS = 300
CEILING_ROLES = (ModelRole.CHAT, ModelRole.CHAT_SPONSORED)
# OpenRouter's provider.max_price (USD per 1M tokens) at this multiple of the price snapshot: routing
# may still pick a pricier provider for speed or uptime, but a swap can't silently multiply the cost.
MAX_PRICE_HEADROOM = 2


def with_ceiling(pin: RolePin, row: ModelPin) -> RolePin:
    """The pin with a ``max_price`` from its price snapshot, unless it sets its own or has no snapshot.

    The ceiling covers the whole request, so OpenRouter skips a fallback priced above it; the retry a
    fallback leads (``RolePin.rotated``) goes without it.
    """
    if row.price_prompt is None or pin.max_price_prompt is not None or pin.max_price_completion is not None:
        return pin
    update: dict[str, object] = {
        "max_price_prompt": float(row.price_prompt) * MAX_PRICE_HEADROOM,
        "derived_ceiling": True,
    }
    if row.price_completion is not None:
        update["max_price_completion"] = float(row.price_completion) * MAX_PRICE_HEADROOM
    return pin.model_copy(update=update)


class PinStore:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.defaults = settings.model_pins
        self._cache: dict[ModelRole, RolePin] = {}
        self._routed: dict[ModelRole, RolePin] = {}
        self._loaded_at = 0.0

    async def seed(self) -> None:
        """Make roles nobody chose follow the code defaults; never overwrite scout or owner changes.

        Missing roles are inserted; a ``seed`` row whose default changed is updated, keeping the old pin as
        ``previous`` (Rollback). A new model drops the price snapshot until the scout takes a fresh one.
        """
        async with self.db.transaction() as session:
            rows = {row.role: row for row in await session.scalars(select(ModelPin).with_for_update())}
            for role, pin in self.defaults.items():
                wanted = pin.model_dump(mode="json")
                row = rows.get(role.value)
                if row is None:
                    session.add(ModelPin(role=role.value, pin=wanted, source="seed"))
                elif row.source == "seed" and row.pin != wanted:
                    if row.pin.get("model") != pin.model:
                        row.price_prompt = row.price_completion = None
                    row.previous, row.pin = row.pin, wanted
                    log.info("model pin for %s follows the new default", role.value)
        self._loaded_at = 0.0

    async def get(self, role: ModelRole) -> RolePin:
        """The pin to route a request with (chat roles carry their price ceiling)."""
        if time.monotonic() - self._loaded_at > CACHE_SECONDS:
            await self.refresh()
        return self._routed.get(role) or self.defaults[role]

    async def refresh(self) -> None:
        cache: dict[ModelRole, RolePin] = {}
        routed: dict[ModelRole, RolePin] = {}
        async with self.db.session() as session:
            for row in await session.scalars(select(ModelPin)):
                try:  # a role retired from code or a malformed pin must not break the other roles
                    role, pin = ModelRole(row.role), RolePin.model_validate(row.pin)
                except ValueError:
                    log.warning("ignoring unusable model pin for role %r", row.role)
                    continue
                cache[role] = pin
                routed[role] = with_ceiling(pin, row) if role in CEILING_ROLES else pin
        self._cache, self._routed = cache, routed
        self._loaded_at = time.monotonic()

    async def all(self) -> dict[ModelRole, RolePin]:
        """The stored pins as chosen (no derived ceiling), for the scout and the owner console."""
        await self.refresh()
        return {role: self._cache.get(role, pin) for role, pin in self.defaults.items()}


async def snapshot_prices[T](session: AsyncSession, role: ModelRole, fallback: T) -> tuple[float, float] | T:
    """The role's price snapshot, USD per 1M prompt and completion units (tokens; characters for speech),
    as the scout took it; ``fallback`` while there is none (a new model, or a role the scout doesn't price)."""
    row = await session.get(ModelPin, role.value)
    if row is None or row.price_prompt is None:
        return fallback
    return float(row.price_prompt), float(row.price_completion or 0)


async def apply_pin(session: AsyncSession, role: ModelRole, pin: RolePin, *, source: str) -> None:
    row = await session.get(ModelPin, role.value)
    if row is None:
        session.add(ModelPin(role=role.value, pin=pin.model_dump(mode="json"), source=source))
        return
    if row.pin != pin.model_dump(mode="json"):
        if row.pin.get("model") != pin.model:  # the snapshot priced the old model; the scout re-prices
            row.price_prompt = row.price_completion = None
        row.previous = row.pin
        row.pin = pin.model_dump(mode="json")
        row.source = source
    row.checked_at = datetime.now(UTC)


async def rollback_pin(session: AsyncSession, role: ModelRole) -> bool:
    row = await session.get(ModelPin, role.value)
    if row is None or row.previous is None:
        return False
    if row.previous.get("model") != row.pin.get("model"):
        row.price_prompt = row.price_completion = None
    row.pin, row.previous = row.previous, row.pin
    row.source = "owner"
    return True
