"""Isolated application, PostgreSQL, and Bot API fixtures for Telegram E2E."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from derp.config import Settings
from derp.db import DatabaseManager
from derp.models import Base
from tests.e2e.harness import TelegramConversation
from tests.e2e.telegram_api import TelegramBotAPIServer


async def _truncate_application_tables(database: DatabaseManager) -> None:
    table_names = [
        database.engine.dialect.identifier_preparer.quote(table.name)
        for table in reversed(Base.metadata.sorted_tables)
    ]
    if not table_names:
        raise RuntimeError("SQLAlchemy model metadata contains no tables")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE")
        )


@pytest_asyncio.fixture
async def e2e_database(migrated_database: str) -> AsyncIterator[DatabaseManager]:
    """Use production session boundaries against an otherwise empty schema."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        raise pytest.UsageError(
            "Telegram E2E owns its test database and must run without pytest-xdist"
        )
    database = DatabaseManager(migrated_database)
    await database.connect()
    try:
        await _truncate_application_tables(database)
        try:
            yield database
        finally:
            await _truncate_application_tables(database)
    finally:
        await database.disconnect()


@pytest_asyncio.fixture
async def telegram_conversation(
    e2e_database: DatabaseManager,
) -> AsyncIterator[TelegramConversation]:
    """Own the local Bot API and every restartable application process."""
    settings = Settings(
        _env_file=None,
        environment="dev",
        telegram_bot_token="123456:TEST_TOKEN_FOR_TESTING",  # noqa: S106
        database_url="unused-by-explicit-test-manager",
        google_api_paid_key="test-google-key",
        logfire_token="test-logfire-token",  # noqa: S106
        operator_ids=[],
    )
    server = TelegramBotAPIServer(
        token=settings.telegram_bot_token.get_secret_value(),
        bot_user={
            "id": settings.bot_id,
            "is_bot": True,
            "first_name": "Derp",
            "username": settings.bot_username,
            "can_read_all_group_messages": True,
        },
    )
    await server.start()
    conversation = TelegramConversation(
        settings=settings,
        database=e2e_database,
        server=server,
    )
    try:
        yield conversation
    finally:
        try:
            await conversation.stop()
        finally:
            await server.close()
            server.assert_clean()
