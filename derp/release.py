"""Read-only database gates for production releases."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

MINIMUM_POSTGRES_MAJOR = 18

LEGACY_SCHEMA = {
    "users": frozenset({"id", "credits"}),
    "chats": frozenset({"id", "type", "credits"}),
    "messages": frozenset({"id", "chat_id"}),
}

CORE_SCHEMA = {
    "users": frozenset({"id", "credits"}),
    "chats": frozenset({"id", "type", "credits", "retention_days"}),
    "messages": frozenset(
        {
            "chat_id",
            "source_snapshot",
            "history_dto",
            "canonical_projection",
            "retention_expires_at",
        }
    ),
    "operation_quotes": frozenset(
        {"operation_id", "provider", "amount_credits", "pricing_version"}
    ),
    "paid_operations": frozenset({"id", "quote_id", "wallet_id", "state"}),
    "wallets": frozenset({"id", "user_id", "chat_id", "debt_credits"}),
    "wallet_lots": frozenset(
        {
            "wallet_id",
            "kind",
            "payment_receipt_id",
            "subscription_cycle_id",
            "granted_credits",
            "available_credits",
            "reserved_credits",
            "consumed_credits",
            "debt_offset_credits",
        }
    ),
    "wallet_debt_sources": frozenset(
        {
            "id",
            "wallet_id",
            "source_wallet_lot_id",
            "incurred_credits",
            "outstanding_credits",
            "recovered_credits",
        }
    ),
    "wallet_debt_repayment_allocations": frozenset(
        {
            "debt_source_id",
            "repayment_wallet_lot_id",
            "wallet_id",
            "allocated_credits",
            "restored_credits",
            "revoked_credits",
        }
    ),
    "wallet_ledger_entries": frozenset(
        {"wallet_id", "wallet_lot_id", "event_type", "amount_credits"}
    ),
    "purchase_intents": frozenset(
        {
            "payer_user_id",
            "terms_acceptance_id",
            "product_id",
            "product_version",
            "stars",
            "status",
        }
    ),
    "payment_receipts": frozenset(
        {"purchase_intent_id", "telegram_charge_id", "total_amount", "status"}
    ),
    "payment_update_inbox": frozenset(
        {
            "telegram_update_id",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "settled_at",
            "attention_reason",
            "reply_attempt_count",
            "reply_status",
        }
    ),
    "payment_refund_requests": frozenset(
        {
            "payment_receipt_id",
            "status",
            "submitted_at",
            "accepted_at",
            "reconciled_at",
        }
    ),
    "subscription_renewal_commands": frozenset(
        {
            "subscription_id",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "completed_at",
            "last_failure_code",
        }
    ),
    "legal_acceptances": frozenset(
        {"id", "user_id", "document", "version", "accepted_at"}
    ),
    "support_requests": frozenset({"id", "requester_user_id", "kind", "status"}),
    "inference_usage_records": frozenset(
        {
            "provider",
            "provider_model_id",
            "actual_model_id",
            "downstream_provider",
            "route_policy_matched",
            "status",
        }
    ),
    "artifacts": frozenset({"operation_id", "storage_key", "expires_at"}),
    "delivery_intents": frozenset(
        {"operation_id", "resend_token_hash", "state", "expires_at"}
    ),
}


class ReleaseMode(StrEnum):
    """Database validation phase in the deployment transaction."""

    PREFLIGHT = "preflight"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    """One content-free release invariant and its aggregate observation."""

    name: str
    passed: bool
    value: bool | int | str | None


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    """Stable machine-readable result returned by the release CLI."""

    mode: ReleaseMode
    checks: tuple[ReleaseCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_json(self) -> str:
        payload = {
            "mode": self.mode.value,
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class MigrationGraph:
    """The single expected head and every revision shipped in this image."""

    head: str
    revisions: frozenset[str]


def load_migration_graph(config_path: Path | None = None) -> MigrationGraph:
    """Load the image's Alembic graph without reading application settings."""
    path = config_path or Path(__file__).resolve().parents[1] / "alembic.ini"
    scripts = ScriptDirectory.from_config(Config(str(path)))
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise RuntimeError("release image must contain exactly one migration head")
    return MigrationGraph(
        head=heads[0],
        revisions=frozenset(script.revision for script in scripts.walk_revisions()),
    )


async def run_release_checks(
    mode: ReleaseMode,
    database_url: str,
    *,
    config_path: Path | None = None,
    expected_legacy_group_history_purge_count: int | None = None,
) -> ReleaseReport:
    """Run one release phase inside a read-only database transaction."""
    if not isinstance(mode, ReleaseMode):
        raise TypeError("mode must be a ReleaseMode")
    if not database_url:
        raise ValueError("database_url is required")
    if (
        mode is ReleaseMode.PREFLIGHT
        and expected_legacy_group_history_purge_count is None
    ):
        raise ValueError("preflight requires the reviewed purge count")
    if expected_legacy_group_history_purge_count is not None and (
        isinstance(expected_legacy_group_history_purge_count, bool)
        or expected_legacy_group_history_purge_count < 0
    ):
        raise ValueError("expected purge count must be non-negative")

    graph = load_migration_graph(config_path)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                return await _collect_report(
                    connection,
                    mode,
                    graph,
                    expected_legacy_group_history_purge_count=(
                        expected_legacy_group_history_purge_count
                    ),
                )
    finally:
        await engine.dispose()


async def _collect_report(
    connection: AsyncConnection,
    mode: ReleaseMode,
    graph: MigrationGraph,
    *,
    expected_legacy_group_history_purge_count: int | None,
) -> ReleaseReport:
    columns = await _schema_columns(connection)
    postgres_version = int(
        await connection.scalar(
            text("SELECT current_setting('server_version_num')::integer")
        )
        or 0
    )
    postgres_major = postgres_version // 10_000
    revisions = await _database_revisions(connection, columns)
    current_revision = revisions[0] if len(revisions) == 1 else None

    checks = [
        ReleaseCheck(
            name="postgres_major",
            passed=postgres_major >= MINIMUM_POSTGRES_MAJOR,
            value=postgres_major,
        ),
        ReleaseCheck(
            name="database_revision",
            passed=(
                len(revisions) == 1
                and current_revision in graph.revisions
                and (mode is ReleaseMode.PREFLIGHT or current_revision == graph.head)
            ),
            value=current_revision,
        ),
    ]

    required_schema = LEGACY_SCHEMA if mode is ReleaseMode.PREFLIGHT else CORE_SCHEMA
    missing_schema_items = _missing_schema_items(columns, required_schema)
    checks.append(
        ReleaseCheck(
            name=(
                "legacy_schema_missing_items"
                if mode is ReleaseMode.PREFLIGHT
                else "core_schema_missing_items"
            ),
            passed=missing_schema_items == 0,
            value=missing_schema_items,
        )
    )

    negative_balances = await _negative_legacy_balances(connection, columns)
    checks.append(
        ReleaseCheck(
            name="negative_legacy_balance_count",
            passed=negative_balances == 0,
            value=negative_balances,
        )
    )

    purge_count = await _legacy_group_history_purge_count(connection, columns)
    checks.append(
        ReleaseCheck(
            name="legacy_group_history_purge_count",
            passed=(
                purge_count is not None
                and (
                    (mode is ReleaseMode.VERIFY and purge_count == 0)
                    or (
                        mode is ReleaseMode.PREFLIGHT
                        and purge_count == expected_legacy_group_history_purge_count
                    )
                )
            ),
            value=purge_count,
        )
    )

    if mode is ReleaseMode.VERIFY:
        shortfall_count = await _wallet_backfill_shortfall_count(connection, columns)
        checks.append(
            ReleaseCheck(
                name="wallet_migration_shortfall_count",
                passed=shortfall_count == 0,
                value=shortfall_count,
            )
        )
        zero_count_checks = (
            (
                "payment_update_attention_count",
                await _payment_update_attention_count(connection, columns),
            ),
            (
                "payment_update_due_count",
                await _payment_update_due_count(connection, columns),
            ),
            (
                "payment_update_reply_failure_count",
                await _payment_update_reply_failure_count(connection, columns),
            ),
            (
                "refund_request_attention_count",
                await _refund_request_attention_count(connection, columns),
            ),
            (
                "refund_request_inflight_count",
                await _refund_request_inflight_count(connection, columns),
            ),
            (
                "subscription_renewal_attention_count",
                await _subscription_renewal_attention_count(connection, columns),
            ),
            (
                "subscription_renewal_due_count",
                await _subscription_renewal_due_count(connection, columns),
            ),
            (
                "inference_route_policy_violation_count",
                await _inference_route_policy_violation_count(connection, columns),
            ),
            (
                "wallet_debt_provenance_inconsistency_count",
                await _wallet_debt_provenance_inconsistency_count(connection, columns),
            ),
        )
        checks.extend(
            ReleaseCheck(name=name, passed=value == 0, value=value)
            for name, value in zero_count_checks
        )

    return ReleaseReport(mode=mode, checks=tuple(checks))


async def _schema_columns(
    connection: AsyncConnection,
) -> dict[str, frozenset[str]]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                """
            )
        )
    ).all()
    columns: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        columns.setdefault(str(table_name), set()).add(str(column_name))
    return {name: frozenset(names) for name, names in columns.items()}


async def _database_revisions(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> tuple[str, ...]:
    if "version_num" not in columns.get("alembic_version", frozenset()):
        return ()
    rows = (
        await connection.execute(text("SELECT version_num FROM alembic_version"))
    ).scalars()
    return tuple(sorted(str(revision) for revision in rows))


def _missing_schema_items(
    actual: Mapping[str, frozenset[str]],
    required: Mapping[str, frozenset[str]],
) -> int:
    return sum(
        len(columns - actual.get(table, frozenset()))
        for table, columns in required.items()
    )


async def _guarded_aggregate_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
    required: Mapping[str, frozenset[str]],
    statement: str,
) -> int | None:
    if _missing_schema_items(columns, required):
        return None
    return int(await connection.scalar(text(statement)) or 0)


async def _negative_legacy_balances(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    if _missing_schema_items(columns, LEGACY_SCHEMA):
        return None
    return int(
        await connection.scalar(
            text(
                """
                SELECT
                    (SELECT count(*) FROM users WHERE credits < 0)
                    + (SELECT count(*) FROM chats WHERE credits < 0)
                """
            )
        )
        or 0
    )


async def _legacy_group_history_purge_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    if _missing_schema_items(columns, LEGACY_SCHEMA):
        return None
    message_columns = columns["messages"]
    if "retention_expires_at" in message_columns:
        return 0
    statement = (
        text(
            """
            SELECT count(*)
            FROM messages
            JOIN chats ON chats.id = messages.chat_id
            WHERE chats.type IN ('group', 'supergroup', 'channel')
              AND messages.source_snapshot = '{}'::jsonb
            """
        )
        if "source_snapshot" in message_columns
        else text(
            """
            SELECT count(*)
            FROM messages
            JOIN chats ON chats.id = messages.chat_id
            WHERE chats.type IN ('group', 'supergroup', 'channel')
            """
        )
    )
    return int(await connection.scalar(statement) or 0)


async def _wallet_backfill_shortfall_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    if _missing_schema_items(columns, CORE_SCHEMA):
        return None
    return int(
        await connection.scalar(
            text(
                """
                WITH legacy_balances AS (
                    SELECT id AS owner_id, 'user' AS owner_kind, credits
                    FROM users
                    WHERE credits > 0
                    UNION ALL
                    SELECT id AS owner_id, 'chat' AS owner_kind, credits
                    FROM chats
                    WHERE credits > 0
                ),
                migrated_balances AS (
                    SELECT
                        coalesce(wallets.user_id, wallets.chat_id) AS owner_id,
                        CASE WHEN wallets.user_id IS NOT NULL THEN 'user' ELSE 'chat' END
                            AS owner_kind,
                        coalesce(sum(wallet_lots.granted_credits) FILTER (
                            WHERE wallet_lots.kind = 'purchased'
                              AND wallet_lots.payment_receipt_id IS NULL
                              AND wallet_lots.subscription_cycle_id IS NULL
                        ), 0) AS credits
                    FROM wallets
                    LEFT JOIN wallet_lots ON wallet_lots.wallet_id = wallets.id
                    GROUP BY wallets.id
                )
                SELECT count(*)
                FROM legacy_balances
                LEFT JOIN migrated_balances USING (owner_id, owner_kind)
                WHERE coalesce(migrated_balances.credits, 0) < legacy_balances.credits
                """
            )
        )
        or 0
    )


async def _payment_update_attention_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {"payment_update_inbox": frozenset({"status", "attention_reason"})}
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM payment_update_inbox
        WHERE status = 'attention'
           OR attention_reason IS NOT NULL
        """,
    )


async def _payment_update_due_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {
        "payment_update_inbox": frozenset(
            {"status", "next_attempt_at", "lease_expires_at"}
        )
    }
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM payment_update_inbox
        WHERE status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP
           OR status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
        """,
    )


async def _payment_update_reply_failure_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {"payment_update_inbox": frozenset({"reply_status"})}
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM payment_update_inbox
        WHERE reply_status IN ('failed', 'skipped')
        """,
    )


async def _refund_request_attention_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {"payment_refund_requests": frozenset({"status"})}
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM payment_refund_requests
        WHERE status = 'needs_review'
        """,
    )


async def _refund_request_inflight_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {"payment_refund_requests": frozenset({"status"})}
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM payment_refund_requests
        WHERE status IN ('submitting', 'accepted')
        """,
    )


async def _subscription_renewal_attention_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {"subscription_renewal_commands": frozenset({"status"})}
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM subscription_renewal_commands
        WHERE status = 'attention'
        """,
    )


async def _subscription_renewal_due_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {
        "subscription_renewal_commands": frozenset(
            {"status", "next_attempt_at", "lease_expires_at"}
        )
    }
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM subscription_renewal_commands
        WHERE status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP
           OR status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
        """,
    )


async def _inference_route_policy_violation_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {"inference_usage_records": frozenset({"route_policy_matched"})}
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
        SELECT count(*)
        FROM inference_usage_records
        WHERE NOT route_policy_matched
        """,
    )


async def _wallet_debt_provenance_inconsistency_count(
    connection: AsyncConnection,
    columns: Mapping[str, frozenset[str]],
) -> int | None:
    required = {
        "wallets": frozenset({"id", "debt_credits"}),
        "wallet_lots": frozenset({"id", "wallet_id", "debt_offset_credits"}),
        "wallet_debt_sources": frozenset(
            {
                "id",
                "wallet_id",
                "incurred_credits",
                "outstanding_credits",
                "recovered_credits",
            }
        ),
        "wallet_debt_repayment_allocations": frozenset(
            {
                "debt_source_id",
                "repayment_wallet_lot_id",
                "allocated_credits",
                "restored_credits",
                "revoked_credits",
            }
        ),
    }
    return await _guarded_aggregate_count(
        connection,
        columns,
        required,
        """
                WITH source_active_repayments AS (
                    SELECT
                        debt_source_id,
                        sum(
                            allocated_credits - restored_credits - revoked_credits
                        ) AS active_credits
                    FROM wallet_debt_repayment_allocations
                    GROUP BY debt_source_id
                ),
                lot_active_repayments AS (
                    SELECT
                        repayment_wallet_lot_id,
                        sum(
                            allocated_credits - restored_credits - revoked_credits
                        ) AS active_credits
                    FROM wallet_debt_repayment_allocations
                    GROUP BY repayment_wallet_lot_id
                ),
                wallet_open_debt AS (
                    SELECT wallet_id, sum(outstanding_credits) AS outstanding_credits
                    FROM wallet_debt_sources
                    GROUP BY wallet_id
                )
                SELECT
                    (
                        SELECT count(*)
                        FROM wallets
                        LEFT JOIN wallet_open_debt
                          ON wallet_open_debt.wallet_id = wallets.id
                        WHERE wallets.debt_credits
                            <> coalesce(wallet_open_debt.outstanding_credits, 0)
                    )
                    + (
                        SELECT count(*)
                        FROM wallet_debt_sources
                        LEFT JOIN source_active_repayments
                          ON source_active_repayments.debt_source_id
                             = wallet_debt_sources.id
                        WHERE wallet_debt_sources.incurred_credits
                            <> wallet_debt_sources.outstanding_credits
                               + wallet_debt_sources.recovered_credits
                               + coalesce(
                                   source_active_repayments.active_credits,
                                   0
                               )
                    )
                    + (
                        SELECT count(*)
                        FROM wallet_lots
                        LEFT JOIN lot_active_repayments
                          ON lot_active_repayments.repayment_wallet_lot_id
                             = wallet_lots.id
                        WHERE wallet_lots.debt_offset_credits
                            <> coalesce(lot_active_repayments.active_credits, 0)
                    )
        """,
    )


def _failure_json(mode: ReleaseMode) -> str:
    return json.dumps(
        {"mode": mode.value, "ok": False, "error": "release_check_failed"},
        separators=(",", ":"),
        sort_keys=True,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run a release gate while keeping every failure content-free."""
    parser = argparse.ArgumentParser(prog="python -m derp.release")
    parser.add_argument("mode", choices=tuple(ReleaseMode), type=ReleaseMode)
    parser.add_argument(
        "--expected-legacy-group-history-purge-count",
        type=int,
    )
    args = parser.parse_args(argv)
    mode: ReleaseMode = args.mode
    output = stdout or sys.stdout
    environment = environ or os.environ

    try:
        database_url = environment["DATABASE_URL"]
        report = asyncio.run(
            run_release_checks(
                mode,
                database_url,
                expected_legacy_group_history_purge_count=(
                    args.expected_legacy_group_history_purge_count
                ),
            )
        )
    except Exception:
        output.write(f"{_failure_json(mode)}\n")
        return 1

    output.write(f"{report.as_json()}\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
