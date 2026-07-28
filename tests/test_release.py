"""Release gates remain read-only, aggregate-only, and upgrade-shaped."""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
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


def test_release_requires_postgres_18() -> None:
    assert release.MINIMUM_POSTGRES_MAJOR == 18


@contextmanager
def _isolated_migration_database(
    database_url: str,
) -> Iterator[tuple[Config, str]]:
    database_name = f"derp_release_{uuid4().hex}"
    assert re.fullmatch(r"derp_release_[0-9a-f]{32}", database_name)
    source_url = make_url(database_url)
    admin_url = source_url.set(database="postgres")
    candidate_url = source_url.set(database=database_name)
    candidate_database_url = candidate_url.render_as_string(hide_password=False)

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

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = candidate_database_url
    config.attributes["configure_logger"] = False
    asyncio.run(create_database())
    try:
        yield config, candidate_database_url
    finally:
        asyncio.run(drop_database())


async def _execute(database_url: str, statements: str | Sequence[str]) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for statement in (
                (statements,) if isinstance(statements, str) else statements
            ):
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _scalar(database_url: str, statement: str) -> object:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(statement))
    finally:
        await engine.dispose()


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
            run_release_checks(
                ReleaseMode.PREFLIGHT,
                release_database_url,
                expected_legacy_group_history_purge_count=2,
            )
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
            run_release_checks(
                ReleaseMode.PREFLIGHT,
                release_database_url,
                expected_legacy_group_history_purge_count=2,
            )
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
            "payment_update_attention_count": 0,
            "payment_update_due_count": 0,
            "payment_update_reply_failure_count": 0,
            "refund_request_attention_count": 0,
            "refund_request_inflight_count": 0,
            "subscription_renewal_attention_count": 0,
            "subscription_renewal_due_count": 0,
            "inference_route_policy_violation_count": 0,
            "wallet_debt_provenance_inconsistency_count": 0,
        }
        assert asyncio.run(remaining_messages()) == (0, 1)
    finally:
        asyncio.run(drop_database())


@pytest.mark.database
@pytest.mark.parametrize(
    "inline_allowance_present",
    [True, False],
    ids=("inline-allowance-present", "inline-allowance-absent"),
)
def test_upgrade_accepts_both_observed_987_schema_shapes(
    database_url: str,
    inline_allowance_present: bool,
) -> None:
    with _isolated_migration_database(database_url) as (config, candidate_url):
        command.upgrade(config, "98772896c1f4")
        if not inline_allowance_present:
            asyncio.run(_execute(candidate_url, "DROP TABLE inline_daily_allowances"))

        before = asyncio.run(
            _scalar(
                candidate_url,
                "SELECT to_regclass('inline_daily_allowances') IS NOT NULL",
            )
        )
        assert before is inline_allowance_present

        command.upgrade(config, "head")

        assert (
            asyncio.run(
                _scalar(candidate_url, "SELECT version_num FROM alembic_version")
            )
            == release.load_migration_graph().head
        )
        assert asyncio.run(
            _scalar(
                candidate_url,
                "SELECT to_regclass('inline_daily_allowances') IS NULL",
            )
        )
        assert asyncio.run(run_release_checks(ReleaseMode.VERIFY, candidate_url)).ok


@pytest.mark.database
def test_release_safety_migration_rejects_unproven_legacy_debt(
    database_url: str,
) -> None:
    with _isolated_migration_database(database_url) as (config, candidate_url):
        command.upgrade(config, "21630de6de88")
        asyncio.run(
            _execute(
                candidate_url,
                """
                INSERT INTO users (
                    id, telegram_id, is_bot, first_name, is_premium, credits
                ) VALUES (
                    gen_random_uuid(), 700002, false, 'Debt gate', false, 0
                )
                """,
            )
        )
        command.upgrade(config, "dcca6f548b5b")
        asyncio.run(
            _execute(
                candidate_url,
                "UPDATE wallets SET debt_credits = 1 WHERE user_id IS NOT NULL",
            )
        )

        with pytest.raises(
            RuntimeError,
            match="legacy wallet debt requires reviewed provenance backfill",
        ):
            command.upgrade(config, "head")

        assert (
            asyncio.run(
                _scalar(candidate_url, "SELECT version_num FROM alembic_version")
            )
            == "dcca6f548b5b"
        )
        assert asyncio.run(
            _scalar(
                candidate_url,
                "SELECT to_regclass('payment_update_inbox') IS NULL",
            )
        )


@pytest.mark.database
def test_verify_blocks_unreconciled_release_safety_state(
    database_url: str,
) -> None:
    with _isolated_migration_database(database_url) as (config, candidate_url):
        command.upgrade(config, "head")
        asyncio.run(
            _execute(
                candidate_url,
                (
                    """
                    WITH new_user AS (
                        INSERT INTO users (
                            id, telegram_id, is_bot, first_name, is_premium, credits
                        ) VALUES (
                            gen_random_uuid(), 700003, false, 'Release gate', false, 0
                        )
                        RETURNING id
                    )
                    INSERT INTO wallets (id, user_id, debt_credits)
                    SELECT gen_random_uuid(), id, 1 FROM new_user
                    """,
                    """
                    INSERT INTO payment_update_inbox (
                        id, telegram_update_id, kind, payload_token_hash,
                        telegram_charge_id, provider_charge_id, payer_telegram_id,
                        reply_chat_id, reply_language, currency, total_amount,
                        status, next_attempt_at, attention_reason, reply_status
                    ) VALUES
                        (
                            gen_random_uuid(), 1, 'successful_payment', repeat('a', 64),
                            'charge-attention', 'provider-attention', 700003, 700003,
                            'en', 'XTR', 1, 'attention', CURRENT_TIMESTAMP,
                            'settlement_conflict', 'sent'
                        ),
                        (
                            gen_random_uuid(), 2, 'successful_payment', repeat('b', 64),
                            'charge-due', 'provider-due', 700003, 700003,
                            'en', 'XTR', 1, 'pending',
                            CURRENT_TIMESTAMP - interval '1 minute', NULL, 'pending'
                        ),
                        (
                            gen_random_uuid(), 3, 'successful_payment', repeat('c', 64),
                            'charge-reply', 'provider-reply', 700003, 700003,
                            'en', 'XTR', 1, 'completed',
                            CURRENT_TIMESTAMP + interval '1 hour', NULL, 'failed'
                        )
                    """,
                    """
                    WITH renewal_users AS (
                        INSERT INTO users (
                            id, telegram_id, is_bot, first_name, is_premium, credits
                        ) VALUES
                            (
                                gen_random_uuid(), 700004, false,
                                'Renewal attention', false, 0
                            ),
                            (
                                gen_random_uuid(), 700005, false,
                                'Renewal due', false, 0
                            ),
                            (
                                gen_random_uuid(), 700006, false,
                                'Renewal expired lease', false, 0
                            )
                        RETURNING id, telegram_id
                    ),
                    renewal_subscriptions AS (
                        INSERT INTO subscriptions (
                            id, user_id, plan_id, plan_version, status,
                            renewal_enabled, current_period_end
                        )
                        SELECT
                            gen_random_uuid(), id, 'personal_monthly', 'v1',
                            'active', true, CURRENT_TIMESTAMP + interval '30 days'
                        FROM renewal_users
                        RETURNING id, user_id
                    )
                    INSERT INTO subscription_renewal_commands (
                        id, subscription_id, payer_telegram_id,
                        telegram_charge_id, desired_enabled, status,
                        attempt_count, next_attempt_at, lease_token,
                        lease_expires_at
                    )
                    SELECT
                        gen_random_uuid(), renewal_subscriptions.id,
                        renewal_users.telegram_id,
                        'renewal-' || renewal_users.telegram_id::text,
                        false,
                        CASE renewal_users.telegram_id
                            WHEN 700004 THEN 'attention'
                            WHEN 700005 THEN 'pending'
                            ELSE 'processing'
                        END,
                        CASE WHEN renewal_users.telegram_id = 700006 THEN 1 ELSE 0 END,
                        CURRENT_TIMESTAMP - interval '1 minute',
                        CASE
                            WHEN renewal_users.telegram_id = 700006
                            THEN gen_random_uuid()
                            ELSE NULL
                        END,
                        CASE
                            WHEN renewal_users.telegram_id = 700006
                            THEN CURRENT_TIMESTAMP - interval '1 minute'
                            ELSE NULL
                        END
                    FROM renewal_subscriptions
                    JOIN renewal_users
                      ON renewal_users.id = renewal_subscriptions.user_id
                    """,
                    """
                    WITH owner AS (
                        SELECT id FROM users WHERE telegram_id = 700003
                    )
                    INSERT INTO inference_usage_records (
                        id, user_id, provider, provider_model_id,
                        route_policy_matched, provider_started_at
                    )
                    SELECT
                        gen_random_uuid(), id, 'openrouter', 'release/model',
                        false, CURRENT_TIMESTAMP
                    FROM owner
                    """,
                    """
                    WITH receipt AS (
                        INSERT INTO payment_receipts (
                            id, telegram_charge_id, provider_charge_id,
                            payer_telegram_id, currency, total_amount,
                            payload_token_hash, status
                        ) VALUES (
                            gen_random_uuid(), 'refund-attention',
                            'provider-refund-attention', 700003, 'XTR', 1,
                            repeat('d', 64), 'refund_requested'
                        )
                        RETURNING id
                    )
                    INSERT INTO payment_refund_requests (
                        id, payment_receipt_id, requester_telegram_id, status,
                        attempt_count, submitted_at, last_error_code
                    )
                    SELECT
                        gen_random_uuid(), id, 700003, 'needs_review', 1,
                        CURRENT_TIMESTAMP, 'ambiguous_provider_result'
                    FROM receipt
                    """,
                    """
                    WITH receipt AS (
                        INSERT INTO payment_receipts (
                            id, telegram_charge_id, provider_charge_id,
                            payer_telegram_id, currency, total_amount,
                            payload_token_hash, status
                        ) VALUES (
                            gen_random_uuid(), 'refund-submitting',
                            'provider-refund-submitting', 700003, 'XTR', 1,
                            repeat('e', 64), 'refund_requested'
                        )
                        RETURNING id
                    )
                    INSERT INTO payment_refund_requests (
                        id, payment_receipt_id, requester_telegram_id, status,
                        attempt_count, submitted_at
                    )
                    SELECT
                        gen_random_uuid(), id, 700003, 'submitting', 1,
                        CURRENT_TIMESTAMP
                    FROM receipt
                    """,
                ),
            )
        )

        report = asyncio.run(run_release_checks(ReleaseMode.VERIFY, candidate_url))

        assert not report.ok
        values = _check_values(report)
        assert values["payment_update_attention_count"] == 1
        assert values["payment_update_due_count"] == 1
        assert values["payment_update_reply_failure_count"] == 1
        assert values["refund_request_attention_count"] == 1
        assert values["refund_request_inflight_count"] == 1
        assert values["subscription_renewal_attention_count"] == 1
        assert values["subscription_renewal_due_count"] == 2
        assert values["inference_route_policy_violation_count"] == 1
        assert values["wallet_debt_provenance_inconsistency_count"] == 1
