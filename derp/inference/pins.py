"""Live model pins: DB-backed, seeded from settings, cached briefly in-process."""

import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.inference.models import ModelPin
from derp.inference.roles import ModelRole, RolePin

CACHE_SECONDS = 300


class PinStore:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.defaults = settings.model_pins
        self._cache: dict[ModelRole, RolePin] = {}
        self._loaded_at = 0.0

    async def seed(self) -> None:
        """Insert missing roles from settings; never overwrite scout or owner changes."""
        async with self.db.transaction() as session:
            existing = set(await session.scalars(select(ModelPin.role)))
            for role, pin in self.defaults.items():
                if role.value not in existing:
                    session.add(ModelPin(role=role.value, pin=pin.model_dump(mode="json"), source="seed"))
        self._loaded_at = 0.0

    async def get(self, role: ModelRole) -> RolePin:
        if time.monotonic() - self._loaded_at > CACHE_SECONDS:
            await self.refresh()
        return self._cache.get(role) or self.defaults[role]

    async def refresh(self) -> None:
        async with self.db.session() as session:
            rows = await session.scalars(select(ModelPin))
            self._cache = {ModelRole(row.role): RolePin.model_validate(row.pin) for row in rows}
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
