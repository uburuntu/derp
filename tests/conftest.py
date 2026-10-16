"""Shared fixtures. Database tests need TEST_DATABASE_URL (``make db-test-up``)."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from derp.config import Settings
from derp.db.engine import Database
from derp.db.metadata import metadata

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def _upgrade(connection: Connection) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def migrated_database() -> AsyncIterator[str]:
    """Rebuild the schema from Alembic once per session — never from metadata."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set (run `make db-test-up`)")
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("drop schema if exists public cascade"))
        await connection.execute(text("create schema public"))
        await connection.run_sync(_upgrade)
    await engine.dispose()
    yield TEST_DATABASE_URL


async def truncate_all(database: Database) -> None:
    names = ", ".join(f'"{table.name}"' for table in reversed(metadata.sorted_tables))
    async with database.engine.begin() as connection:
        await connection.execute(text(f"truncate table {names} restart identity cascade"))


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[Database]:
    db = Database(migrated_database, pool_size=5)
    await truncate_all(db)
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def settings() -> Iterator[Settings]:
    yield Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        telegram_bot_token="777000111:TEST_TOKEN_FOR_TESTING_ONLY_0000000",
        database_url=TEST_DATABASE_URL or "postgresql+asyncpg://unused/unused",
        openrouter_api_key="sk-or-test",
        owner_ids=[1000],
    )
