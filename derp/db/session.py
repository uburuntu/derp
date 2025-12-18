"""Database session management for async SQLAlchemy."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseManager:
    """Manages async database connections and sessions.

    Provides a singleton-like pattern for database access with proper
    lifecycle management (connect/disconnect) and session context managers.
    """

    def __init__(self, database_url: str, *, echo: bool = False):
        self._database_url = database_url
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._logger = logging.getLogger(__name__)

    async def connect(self) -> None:
        """Initialize the database engine and session factory."""
        if self._engine is not None:
            return

        self._engine = create_async_engine(
            self._database_url,
            echo=self._echo,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        self._logger.info("Connected to PostgreSQL database")

    async def disconnect(self) -> None:
        """Close the database engine and cleanup resources."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._logger.info("Disconnected from PostgreSQL database")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Get a database session context manager.

        Usage:
            async with db.session() as session:
                result = await session.execute(...)
        """
        if self._session_factory is None:
            await self.connect()

        if self._session_factory is None:
            raise RuntimeError("Failed to initialize database session factory")

        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @property
    def engine(self) -> AsyncEngine:
        """Get the underlying engine (for Alembic migrations)."""
        if self._engine is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._engine


# Singleton instance
_db_manager: DatabaseManager | None = None


def init_db_manager(database_url: str, *, echo: bool = False) -> DatabaseManager:
    """Initialize the global database manager."""
    global _db_manager
    _db_manager = DatabaseManager(database_url, echo=echo)
    return _db_manager


def get_db_manager() -> DatabaseManager:
    """Get the global database manager instance."""
    if _db_manager is None:
        raise RuntimeError(
            "Database manager not initialized. Call init_db_manager() first."
        )
    return _db_manager
