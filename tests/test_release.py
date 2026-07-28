"""Release gates remain read-only, aggregate-only, and upgrade-shaped."""

from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from derp import release
from derp.release import ReleaseMode, run_release_checks


def _check_values(report: release.ReleaseReport) -> dict[str, object]:
    return {check.name: check.value for check in report.checks}


def test_cli_failure_does_not_echo_exception_or_database_url(monkeypatch) -> None:
    database_url = "postgresql+asyncpg://release:private@db/release"

    async def fail(*_args: object, **_kwargs: object) -> release.ReleaseReport:
        raise RuntimeError("private message text")

    monkeypatch.setattr(release, "run_release_checks", fail)
    output = io.StringIO()

    status = release.main(
        ["preflight"],
        environ={"DATABASE_URL": database_url},
        stdout=output,
    )

    assert status == 1
    assert json.loads(output.getvalue()) == {
        "mode": "preflight",
        "ok": False,
        "error": "release_check_failed",
    }
    assert database_url not in output.getvalue()
    assert "private message text" not in output.getvalue()


@pytest.mark.database
def test_release_checks_cover_legacy_upgrade_and_wallet_backfill(
    database_url: str,
) -> None:
    database_name = f"derp_release_{uuid4().hex}"
    assert re.fullmatch(r"derp_release_[0-9a-f]{32}", database_name)
    source_url = make_url(database_url)
    admin_url = source_url.set(database="postgres")
    release_url = source_url.set(database=database_name)
    release_database_url = release_url.render_as_string(hide_password=False)

    async def create_database() -> None:
        engine = create_async_engine(
            admin_url,
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql(
                    f'CREATE DATABASE "{database_name}"'  # noqa: S608
                )
        finally:
            await engine.dispose()

    async def drop_database() -> None:
        engine = create_async_engine(
            admin_url,
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        """
                        SELECT pg_terminate_backend(pid)
                        FROM pg_stat_activity
                        WHERE datname = :database_name
                          AND pid <> pg_backend_pid()
                        """
                    ),
                    {"database_name": database_name},
                )
                await connection.exec_driver_sql(
                    f'DROP DATABASE "{database_name}"'  # noqa: S608
                )
        finally:
            await engine.dispose()

    async def seed_legacy_database() -> None:
        engine = create_async_engine(release_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                user_id = uuid4()
                group_id = uuid4()
                private_chat_id = uuid4()
                await connection.execute(
                    text(
                        """
                        INSERT INTO users (
                            id, telegram_id, is_bot, first_name, is_premium, credits
                        ) VALUES (:id, 700001, false, 'Release', false, 13)
                        """
                    ),
                    {"id": user_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO chats (
                            id, telegram_id, type, is_forum, credits
                        ) VALUES
                            (:group_id, -700001, 'supergroup', false, 21),
                            (:private_id, 700001, 'private', false, 0)
                        """
                    ),
                    {"group_id": group_id, "private_id": private_chat_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO messages (
                            id, chat_id, user_id, telegram_message_id, direction,
                            content_type, text, telegram_date
                        ) VALUES
                            (:first, :group_id, :user_id, 1, 'in', 'text',
                             'legacy group content', now()),
                            (:second, :group_id, :user_id, 2, 'in', 'text',
                             'legacy group content', now()),
                            (:third, :private_id, :user_id, 3, 'in', 'text',
                             'legacy private content', now())
                        """
                    ),
                    {
                        "first": uuid4(),
                        "second": uuid4(),
                        "third": uuid4(),
                        "group_id": group_id,
                        "private_id": private_chat_id,
                        "user_id": user_id,
                    },
                )
        finally:
            await engine.dispose()

    async def set_user_credits(value: int) -> None:
        engine = create_async_engine(release_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE users SET credits = :value"), {"value": value}
                )
        finally:
            await engine.dispose()

    async def remaining_messages() -> tuple[int, int]:
        engine = create_async_engine(release_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                group_count = int(
                    await connection.scalar(
                        text(
                            """
                            SELECT count(*)
                            FROM messages
                            JOIN chats ON chats.id = messages.chat_id
                            WHERE chats.type = 'supergroup'
                            """
                        )
                    )
                    or 0
                )
                private_count = int(
                    await connection.scalar(
                        text(
                            """
                            SELECT count(*)
                            FROM messages
                            JOIN chats ON chats.id = messages.chat_id
                            WHERE chats.type = 'private'
                            """
                        )
                    )
                    or 0
                )
                return group_count, private_count
        finally:
            await engine.dispose()

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = release_database_url
    config.attributes["configure_logger"] = False

    asyncio.run(create_database())
    try:
        command.upgrade(config, "21630de6de88")
        asyncio.run(seed_legacy_database())

        preflight = asyncio.run(
            run_release_checks(ReleaseMode.PREFLIGHT, release_database_url)
        )
        assert preflight.ok
        assert _check_values(preflight) == {
            "postgres_major": 18,
            "database_revision": "21630de6de88",
            "legacy_schema_missing_items": 0,
            "negative_legacy_balance_count": 0,
            "legacy_group_history_purge_count": 2,
        }

        asyncio.run(set_user_credits(-1))
        blocked = asyncio.run(
            run_release_checks(ReleaseMode.PREFLIGHT, release_database_url)
        )
        assert not blocked.ok
        assert _check_values(blocked)["negative_legacy_balance_count"] == 1
        asyncio.run(set_user_credits(13))

        command.upgrade(config, "head")
        verification = asyncio.run(
            run_release_checks(ReleaseMode.VERIFY, release_database_url)
        )
        assert verification.ok
        assert _check_values(verification) == {
            "postgres_major": 18,
            "database_revision": release.load_migration_graph().head,
            "core_schema_missing_items": 0,
            "negative_legacy_balance_count": 0,
            "legacy_group_history_purge_count": 0,
            "wallet_migration_shortfall_count": 0,
        }
        assert asyncio.run(remaining_messages()) == (0, 1)
    finally:
        asyncio.run(drop_database())
