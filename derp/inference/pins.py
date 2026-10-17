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


class PinStore:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.defaults = settings.model_pins
        self._cache: dict[ModelRole, RolePin] = {}
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
        if time.monotonic() - self._loaded_at > CACHE_SECONDS:
            await self.refresh()
        return self._cache.get(role) or self.defaults[role]

    async def refresh(self) -> None:
        cache: dict[ModelRole, RolePin] = {}
        async with self.db.session() as session:
            for row in await session.scalars(select(ModelPin)):
                try:  # a role retired from code or a malformed pin must not break the other roles
                    cache[ModelRole(row.role)] = RolePin.model_validate(row.pin)
                except ValueError:
                    log.warning("ignoring unusable model pin for role %r", row.role)
        self._cache = cache
        self._loaded_at = time.monotonic()

    async def all(self) -> dict[ModelRole, RolePin]:
        await self.refresh()
        return {role: self._cache.get(role, pin) for role, pin in self.defaults.items()}


async def apply_pin(session: AsyncSession, role: ModelRole, pin: RolePin, *, source: str) -> None:
    row = await session.get(ModelPin, role.value)
    if row is None:
        session.add(ModelPin(role=role.value, pin=pin.model_dump(mode="json"), source=source))
        return
    if row.pin != pin.model_dump(mode="json"):
        row.previous = row.pin
        row.pin = pin.model_dump(mode="json")
        row.source = source
    row.checked_at = datetime.now(UTC)


async def rollback_pin(session: AsyncSession, role: ModelRole) -> bool:
    row = await session.get(ModelPin, role.value)
    if row is None or row.previous is None:
        return False
    row.pin, row.previous = row.previous, row.pin
    row.source = "owner"
    return True
