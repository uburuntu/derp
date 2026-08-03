"""Contracts proving Alembic, not ORM table creation, owns the test schema."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from derp.models import Base, Chat, User

pytestmark = pytest.mark.database


def _normalize_sql(expression: object) -> str:
    return " ".join(str(expression).lower().split())


@pytest.mark.asyncio
async def test_database_revision_matches_single_alembic_head(
    db_engine: AsyncEngine,
    alembic_config: Config,
) -> None:
    scripts = ScriptDirectory.from_config(alembic_config)
    expected_heads = set(scripts.get_heads())

    async with db_engine.connect() as connection:
        current_heads = await connection.run_sync(
            lambda sync_connection: set(
                MigrationContext.configure(sync_connection).get_current_heads()
            )
        )

    assert len(expected_heads) == 1
    assert current_heads == expected_heads


def test_alembic_autogenerate_reports_no_schema_drift(
    alembic_config: Config,
    migrated_database: str,
) -> None:
    assert migrated_database
    root_handlers = logging.getLogger().handlers[:]
    command.check(alembic_config)
    assert logging.getLogger().handlers == root_handlers


@pytest.mark.asyncio
async def test_reflected_constraints_match_orm_contract(
    db_engine: AsyncEngine,
) -> None:
    def reflect_constraints(
        sync_connection: Connection,
    ) -> tuple[dict[str, dict[str, str]], dict[str, tuple[str, ...]]]:
        inspector = inspect(sync_connection)
        checks = {
            table.name: {
                str(constraint["name"]): _normalize_sql(constraint["sqltext"])
                for constraint in inspector.get_check_constraints(table.name)
            }
            for table in Base.metadata.sorted_tables
        }
        primary_keys = {
            table.name: tuple(
                inspector.get_pk_constraint(table.name)["constrained_columns"]
            )
            for table in Base.metadata.sorted_tables
        }
        return checks, primary_keys

    async with db_engine.connect() as connection:
        actual_checks, actual_primary_keys = await connection.run_sync(
            reflect_constraints
        )

    expected_checks = {
        table.name: {
            str(constraint.name): _normalize_sql(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        for table in Base.metadata.sorted_tables
    }
    expected_primary_keys = {
        table.name: tuple(column.name for column in table.primary_key.columns)
        for table in Base.metadata.sorted_tables
    }

    assert actual_checks == expected_checks
    assert actual_primary_keys == expected_primary_keys


@pytest.mark.parametrize(
    "record",
    [
        User(telegram_id=9_000_001, is_bot=False, first_name="Negative", credits=-1),
        Chat(telegram_id=-9_000_001, type="group", credits=-1),
    ],
    ids=["user", "chat"],
)
@pytest.mark.asyncio
async def test_migrated_schema_rejects_negative_credits(
    db_session: AsyncSession,
    record: User | Chat,
) -> None:
    db_session.add(record)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (
            """
            INSERT INTO users (id, telegram_id, is_bot, first_name, is_premium)
            VALUES (:id, :telegram_id, false, 'Server Default', false)
            RETURNING credits
            """,
            {"id": uuid4(), "telegram_id": 9_000_002},
        ),
        (
            """
            INSERT INTO chats (id, telegram_id, type, is_forum)
            VALUES (:id, :telegram_id, 'group', false)
            RETURNING credits
            """,
            {"id": uuid4(), "telegram_id": -9_000_002},
        ),
    ],
    ids=["user", "chat"],
)
@pytest.mark.asyncio
async def test_migrated_schema_supplies_credit_server_defaults(
    db_session: AsyncSession,
    statement: str,
    parameters: dict[str, int | UUID],
) -> None:
    balance = await db_session.scalar(text(statement), parameters)
    assert balance == 0
