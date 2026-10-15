import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from derp.db.engine import Database
from derp.db.metadata import metadata
from tests.conftest import alembic_config


def test_single_head() -> None:
    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1


@pytest.mark.db
async def test_models_match_migrations(database: Database) -> None:
    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection, opts={"compare_server_default": True})
        return compare_metadata(context, metadata)

    async with database.engine.connect() as connection:
        assert await connection.run_sync(diff) == []
