"""Alembic environment: async engine from settings, or a connection passed by tests."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from derp.db.metadata import metadata

config = context.config


def run(connection: Connection) -> None:
    """Each revision commits on its own, so one can step out of its transaction for slow work
    (``op.get_context().autocommit_block()``: validating a constraint, building an index concurrently)."""
    # A migration queued behind a lock would block the running release's queries: give up instead.
    # Set for the session, so autocommit blocks keep it; committed before Alembic takes over transactions.
    connection.exec_driver_sql("set lock_timeout = '15s'")
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_server_default=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    from derp.config import get_settings

    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported")
elif (connection := config.attributes.get("connection")) is not None:
    run(connection)
else:
    asyncio.run(run_online())
