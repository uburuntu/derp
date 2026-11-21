import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

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


@pytest.mark.db
async def test_fractional_migration_reconciles_holds_and_immutable_receipts(database: Database) -> None:
    """Upgrade a private copy of 0040 while the normal test schema stays at head."""
    async with database.engine.connect() as conn:
        await conn.exec_driver_sql("CREATE SCHEMA wallet_upgrade")
        try:
            await conn.exec_driver_sql("SET search_path = wallet_upgrade, public")
            await conn.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
            await conn.commit()

            def upgrade(connection: Connection, revision: str) -> None:
                cfg = alembic_config()
                cfg.attributes["connection"] = connection
                command.upgrade(cfg, revision)

            await conn.run_sync(upgrade, "0040")
            # One live hold, one settled hold and one expired lot. Historical rows use the old
            # writer's unmodified column names and are recorded under their original IDs.
            seeds = (
                "INSERT INTO wallets (kind,owner_id) VALUES ('chat',-111)",
                """INSERT INTO credit_lots (wallet_id,operation_id,source,amount,remaining,expires_at)
                   VALUES(1,'purchase','purchase',650,546,NULL),(1,'old-day','sponsored',10,0,now()-'1 day'::interval)""",
                """INSERT INTO reservations(wallet_id,operation_id,amount,captured,state)
                   VALUES(1,'live',100,0,'held'), (1,'done',20,4,'captured')""",
                "INSERT INTO reservation_holds VALUES (1,1,100),(2,1,20)",
                """INSERT INTO ledger_entries(wallet_id,lot_id,operation_id,kind,amount) VALUES
                   (1,1,'purchase','credit',650),(1,1,'live','hold',100),(1,1,'done','hold',20),
                   (1,1,'done','capture',4),(1,1,'done','release',16),
                   (1,2,'old-day','credit',10),(1,2,'old-day','expire',10)""",
            )
            for statement in seeds:
                await conn.exec_driver_sql(statement)
            # A discrepancy must stop the rollout without advancing its revision. Correcting
            # the old column and retrying also exercises recovery after the committed backfill.
            await conn.exec_driver_sql("UPDATE credit_lots SET remaining=545 WHERE id=1")
            await conn.commit()
            with pytest.raises(DBAPIError, match="reconciliation failed"):
                await conn.run_sync(upgrade, "0041")
            await conn.rollback()
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0040"
            await conn.exec_driver_sql("UPDATE credit_lots SET remaining=546 WHERE id=1")
            await conn.commit()
            await conn.run_sync(upgrade, "0041")
            lots = await conn.exec_driver_sql("SELECT amount_dc,remaining_dc FROM credit_lots ORDER BY id")
            assert lots.all() == [(650, 546), (10, 0)]
            holds = await conn.exec_driver_sql("SELECT amount_dc,captured_dc FROM reservations ORDER BY id")
            assert holds.all() == [(100, 0), (20, 4)]
            assert await conn.scalar(text("SELECT count(*) FROM ledger_entries WHERE amount_dc=amount")) == 7
            await conn.exec_driver_sql("UPDATE credit_lots SET remaining=545 WHERE id=1")
            assert await conn.scalar(text("SELECT remaining_dc FROM credit_lots WHERE id=1")) == 545
            # Restore consistency just as the still-running old poller would, then move a fraction.
            await conn.exec_driver_sql(
                "INSERT INTO ledger_entries(wallet_id,lot_id,operation_id,kind,amount) "
                "VALUES(1,1,'transition','hold',1)"
            )
            await conn.exec_driver_sql("UPDATE credit_lots SET remaining_dc=544.987655 WHERE id=1")
            await conn.exec_driver_sql(
                "INSERT INTO ledger_entries(wallet_id,lot_id,operation_id,kind,amount_dc) "
                "VALUES(1,1,'micro','hold',0.012345)"
            )
            await conn.commit()
            with pytest.raises(DBAPIError, match="decimal-aware"):
                await conn.exec_driver_sql(
                    "INSERT INTO ledger_entries(wallet_id,lot_id,operation_id,kind,amount) "
                    "VALUES(1,1,'old-caller','hold',1)"
                )
            await conn.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.exec_driver_sql("UPDATE ledger_entries SET amount_dc=amount_dc+1 WHERE id=1")
            await conn.rollback()
        finally:
            await conn.rollback()
            await conn.exec_driver_sql("RESET search_path")
            await conn.exec_driver_sql("DROP SCHEMA wallet_upgrade CASCADE")
            await conn.commit()


@pytest.mark.db
async def test_reply_rule_migrations_resume_after_interrupted_ddl_and_concurrent_index(
    database: Database,
) -> None:
    """Use actual competing transactions: a cancelled concurrent build leaves an invalid index in pg_index."""

    def migrate(connection: Connection, revision: str, *, down: bool = False) -> None:
        cfg = alembic_config()
        cfg.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(cfg, revision)

    version = text("SELECT version_num FROM alembic_version")
    valid_index = text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)")

    async with database.engine.connect() as conn, database.engine.connect() as blocker:
        await conn.exec_driver_sql("CREATE SCHEMA rule_upgrade")
        try:
            await conn.exec_driver_sql("SET search_path = rule_upgrade, public")
            await conn.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
            await conn.commit()
            await conn.run_sync(migrate, "0041")
            await conn.exec_driver_sql("SET statement_timeout = '500ms'")
            await conn.commit()
            await blocker.exec_driver_sql("LOCK TABLE rule_upgrade.provider_calls IN ACCESS EXCLUSIVE MODE")
            with pytest.raises(DBAPIError, match="statement timeout"):
                await conn.run_sync(migrate, "0042")
            await blocker.rollback()
            await conn.rollback()
            assert await conn.scalar(version) == "0041"
            assert await conn.scalar(text("SELECT to_regclass('rule_upgrade.reply_rules')")) is None
            await conn.run_sync(migrate, "0043")

            # PostgreSQL commits the CREATE INDEX CONCURRENTLY catalog row before it waits for old
            # writers. Timing out at that wait leaves a real invalid index and the prior valid index.
            await blocker.exec_driver_sql("LOCK TABLE rule_upgrade.jobs IN ROW EXCLUSIVE MODE")
            with pytest.raises(DBAPIError, match="statement timeout"):
                await conn.run_sync(migrate, "0044")
            await blocker.rollback()
            await conn.rollback()
            assert await conn.scalar(version) == "0043"
            assert await conn.scalar(valid_index, {"name": "rule_upgrade.ix_jobs_rule_id"}) is False
            stable = await conn.scalar(
                text("SELECT 'rule_upgrade.ix_provider_calls_reservation_id'::regclass::oid")
            )
            await conn.exec_driver_sql("RESET statement_timeout")
            await conn.run_sync(migrate, "0044")
            assert await conn.scalar(version) == "0044"
            assert await conn.scalar(valid_index, {"name": "rule_upgrade.ix_jobs_rule_id"}) is True
            assert (
                await conn.scalar(
                    text("SELECT 'rule_upgrade.ix_provider_calls_reservation_id'::regclass::oid")
                )
                == stable
            )
            await conn.run_sync(migrate, "0041", down=True)
            assert await conn.scalar(text("SELECT to_regclass('rule_upgrade.reply_rules')")) is None
            assert await conn.scalar(
                text(
                    "SELECT convalidated FROM pg_constraint WHERE conrelid='rule_upgrade.messages'::regclass AND conname='ck_messages_kind'"
                )
            )
            await conn.run_sync(migrate, "0044")
        finally:
            await blocker.rollback()
            await conn.rollback()
            await conn.exec_driver_sql("RESET statement_timeout")
            await conn.exec_driver_sql("RESET search_path")
            await conn.exec_driver_sql("DROP SCHEMA rule_upgrade CASCADE")
            await conn.commit()
