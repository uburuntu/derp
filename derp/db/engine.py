"""Async engine and session factory with production session boundaries."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class Database:
    """Owns one engine; every unit of work opens its own short session."""

    def __init__(self, url: str, *, pool_size: int = 10, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, echo=echo
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session for reads; call ``commit`` explicitly for writes."""
        async with self.sessions() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A session inside one transaction that commits on success and rolls back on error."""
        async with self.sessions() as session, session.begin():
            yield session

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("select 1"))

    async def close(self) -> None:
        await self.engine.dispose()
