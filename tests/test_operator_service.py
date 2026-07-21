"""Aggregate and maintenance contracts for the private operator console."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from derp.approvals.maintenance import DeferredApprovalExpiryWorker
from derp.billing import SubscriptionExpiryWorker
from derp.db import DatabaseManager
from derp.delivery import DeliveryMaintenanceReport, DeliveryMaintenanceWorker
from derp.history.retention import HistoryRetentionWorker
from derp.models import Message as MessageModel
from derp.models import Subscription, Wallet, WalletLot
from derp.operations import (
    OperationReconciliationReport,
    OperationReconciliationWorker,
)
from derp.operator import (
    OperatorActivityTotals,
    OperatorConsoleService,
    OperatorMaintenanceAction,
    OperatorSubscriptionTotals,
)


class MutableClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FailingDatabase:
    @asynccontextmanager
    async def read_session(self):
        raise RuntimeError("private database failure")
        yield  # pragma: no cover


class CancelledDatabase:
    @asynccontextmanager
    async def read_session(self):
        raise asyncio.CancelledError
        yield  # pragma: no cover


class SessionDatabase:
    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        self._session = session
        self.engine = engine

    @asynccontextmanager
    async def read_session(self):
        yield self._session


def operation_report() -> OperationReconciliationReport:
    return OperationReconciliationReport(
        examined_count=3,
        expired_quote_count=1,
        incomplete_result_count=1,
        race_skipped_count=1,
    )


def delivery_report() -> DeliveryMaintenanceReport:
    return DeliveryMaintenanceReport(
        interrupted_count=1,
        retry_candidate_count=2,
        delivered_count=1,
        uncertain_count=1,
        expired_count=1,
        artifact_examined_count=2,
        artifact_purged_count=2,
    )


def build_service(
    db: object | None = None,
    *,
    clock: MutableClock | None = None,
) -> tuple[OperatorConsoleService, dict[OperatorMaintenanceAction, AsyncMock]]:
    sweeps = {
        OperatorMaintenanceAction.HISTORY: AsyncMock(return_value=2),
        OperatorMaintenanceAction.SUBSCRIPTIONS: AsyncMock(return_value=3),
        OperatorMaintenanceAction.OPERATIONS: AsyncMock(
            return_value=operation_report()
        ),
        OperatorMaintenanceAction.DELIVERIES: AsyncMock(return_value=delivery_report()),
        OperatorMaintenanceAction.APPROVALS: AsyncMock(return_value=5),
    }
    history = SimpleNamespace(
        is_running=True,
        sweep=sweeps[OperatorMaintenanceAction.HISTORY],
    )
    subscriptions = SimpleNamespace(
        is_running=True,
        sweep=sweeps[OperatorMaintenanceAction.SUBSCRIPTIONS],
    )
    operations = SimpleNamespace(
        is_running=False,
        sweep=sweeps[OperatorMaintenanceAction.OPERATIONS],
    )
    deliveries = SimpleNamespace(
        is_running=True,
        sweep=sweeps[OperatorMaintenanceAction.DELIVERIES],
    )
    approvals = SimpleNamespace(
        is_running=False,
        sweep=sweeps[OperatorMaintenanceAction.APPROVALS],
    )
    monotonic = clock or MutableClock()
    return (
        OperatorConsoleService(
            cast(DatabaseManager, db or MagicMock(spec=DatabaseManager)),
            history_retention=cast(HistoryRetentionWorker, history),
            subscription_expiry=cast(SubscriptionExpiryWorker, subscriptions),
            operation_reconciliation=cast(
                OperationReconciliationWorker,
                operations,
            ),
            delivery_maintenance=cast(DeliveryMaintenanceWorker, deliveries),
            approval_expiry=cast(DeferredApprovalExpiryWorker, approvals),
            monotonic_clock=monotonic,
            utc_clock=lambda: datetime(2026, 7, 21, tzinfo=UTC),
        ),
        sweeps,
    )


async def test_snapshot_preserves_runtime_state_when_database_is_degraded() -> None:
    clock = MutableClock(10.0)
    service, _ = build_service(FailingDatabase(), clock=clock)
    clock.now = 13.5

    with patch("derp.operator.service.report_exception") as report:
        snapshot = await service.snapshot()

    assert snapshot.database.is_degraded
    assert snapshot.runtime.uptime_seconds == 3.5
    assert [worker.running for worker in snapshot.runtime.workers] == [
        True,
        True,
        False,
        True,
        False,
    ]
    report.assert_called_once()
    assert report.call_args.args == ("operator.snapshot_degraded",)
    assert report.call_args.kwargs["level"] == "warning"


async def test_snapshot_propagates_cancellation_without_reporting() -> None:
    service, _ = build_service(CancelledDatabase())

    with (
        patch("derp.operator.service.report_exception") as report,
        pytest.raises(asyncio.CancelledError),
    ):
        await service.snapshot()

    report.assert_not_called()


@pytest.mark.parametrize(
    ("action", "expected_name", "expected_value"),
    [
        (OperatorMaintenanceAction.HISTORY, "purged_message_count", 2),
        (OperatorMaintenanceAction.SUBSCRIPTIONS, "expired_cycle_count", 3),
        (OperatorMaintenanceAction.OPERATIONS, "examined_count", 3),
        (OperatorMaintenanceAction.DELIVERIES, "retry_candidate_count", 2),
        (OperatorMaintenanceAction.APPROVALS, "expired_request_count", 5),
    ],
)
async def test_maintenance_dispatches_only_the_exact_worker(
    action: OperatorMaintenanceAction,
    expected_name: str,
    expected_value: int,
) -> None:
    service, sweeps = build_service()

    with patch("derp.operator.service.logfire.info") as info:
        result = await service.run_maintenance(action, actor_id=42)

    assert result.requested_action is action
    assert len(result.passes) == 1
    counts = {item.name: item.count for item in result.passes[0].counts}
    assert counts[expected_name] == expected_value
    for candidate, sweep in sweeps.items():
        assert sweep.await_count == (1 if candidate is action else 0)
    info.assert_called_once()
    assert info.call_args.args == ("operator.maintenance_pass_completed",)
    assert info.call_args.kwargs["actor_id"] == 42
    assert info.call_args.kwargs["completion"] == "completed_pass"


async def test_all_maintenance_runs_each_exact_worker_once() -> None:
    service, sweeps = build_service()

    with patch("derp.operator.service.logfire.info"):
        result = await service.run_maintenance(
            OperatorMaintenanceAction.ALL,
            actor_id=42,
        )

    assert [item.action for item in result.passes] == [
        OperatorMaintenanceAction.HISTORY,
        OperatorMaintenanceAction.SUBSCRIPTIONS,
        OperatorMaintenanceAction.OPERATIONS,
        OperatorMaintenanceAction.DELIVERIES,
        OperatorMaintenanceAction.APPROVALS,
    ]
    assert all(sweep.await_count == 1 for sweep in sweeps.values())


async def test_maintenance_serializes_different_worker_actions() -> None:
    service, sweeps = build_service()
    history_started = asyncio.Event()
    release_history = asyncio.Event()

    async def block_history() -> int:
        history_started.set()
        await release_history.wait()
        return 0

    sweeps[OperatorMaintenanceAction.HISTORY].side_effect = block_history
    first = asyncio.create_task(
        service.run_maintenance(OperatorMaintenanceAction.HISTORY, actor_id=42)
    )
    await history_started.wait()
    second = asyncio.create_task(
        service.run_maintenance(OperatorMaintenanceAction.SUBSCRIPTIONS, actor_id=42)
    )
    await asyncio.sleep(0)

    assert not sweeps[OperatorMaintenanceAction.SUBSCRIPTIONS].await_count

    release_history.set()
    with patch("derp.operator.service.logfire.info"):
        await asyncio.gather(first, second)
    assert sweeps[OperatorMaintenanceAction.SUBSCRIPTIONS].await_count == 1


@pytest.mark.database
async def test_snapshot_reads_real_aggregate_columns_only(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    user_factory,
    chat_factory,
) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    service, _ = build_service(SessionDatabase(db_session, db_engine))
    baseline = await service.snapshot()
    assert not baseline.database.is_degraded
    baseline_users = baseline.database.users
    baseline_chats = baseline.database.chats
    baseline_messages = baseline.database.retained_messages
    baseline_wallet = baseline.database.wallet
    baseline_subscriptions = baseline.database.subscriptions
    assert baseline_users is not None
    assert baseline_chats is not None
    assert baseline_messages is not None
    assert baseline_wallet is not None
    assert baseline_subscriptions is not None

    recent_user = await user_factory(telegram_id=71)
    old_user = await user_factory(telegram_id=72)
    recent_chat = await chat_factory(telegram_id=-71, chat_type="group")
    old_chat = await chat_factory(telegram_id=-72, chat_type="channel")
    old_user.updated_at = now - timedelta(days=2)
    old_chat.updated_at = now - timedelta(days=2)
    recent_user.updated_at = now
    recent_chat.updated_at = now
    db_session.add_all(
        [
            Subscription(
                user_id=recent_user.id,
                plan_id="operator-test",
                plan_version="v1",
                status="active",
                renewal_enabled=True,
                current_period_end=now + timedelta(days=10),
            ),
            Subscription(
                user_id=old_user.id,
                plan_id="operator-test",
                plan_version="v1",
                status="canceled",
                renewal_enabled=False,
                current_period_end=now + timedelta(days=5),
                canceled_at=now,
            ),
            MessageModel(
                chat_id=recent_chat.id,
                user_id=recent_user.id,
                telegram_message_id=1,
                direction="in",
                role="user",
                capture_kind="explicit",
                source_snapshot={},
                history_dto={},
                canonical_projection={},
                content_type="text",
                text="private recent content",
                telegram_date=now,
            ),
            MessageModel(
                chat_id=old_chat.id,
                user_id=old_user.id,
                telegram_message_id=2,
                direction="in",
                role="user",
                capture_kind="explicit",
                source_snapshot={},
                history_dto={},
                canonical_projection={},
                content_type="text",
                text="private old content",
                telegram_date=now - timedelta(days=2),
            ),
        ]
    )
    wallet = Wallet(user_id=recent_user.id, debt_credits=7)
    db_session.add(wallet)
    await db_session.flush()
    db_session.add(
        WalletLot(
            wallet_id=wallet.id,
            kind="purchased",
            granted_credits=10,
            available_credits=4,
            reserved_credits=2,
            consumed_credits=4,
            expired_credits=0,
            clawed_back_credits=0,
            debt_offset_credits=0,
        )
    )
    await db_session.flush()
    snapshot = await service.snapshot()

    assert not snapshot.database.is_degraded
    assert snapshot.database.users == OperatorActivityTotals(
        total=baseline_users.total + 2,
        recent_24h=baseline_users.recent_24h + 1,
    )
    assert snapshot.database.chats == OperatorActivityTotals(
        total=baseline_chats.total + 2,
        recent_24h=baseline_chats.recent_24h + 1,
    )
    assert snapshot.database.retained_messages == OperatorActivityTotals(
        total=baseline_messages.total + 2,
        recent_24h=baseline_messages.recent_24h + 1,
    )
    assert snapshot.database.wallet is not None
    assert snapshot.database.wallet.available_credits == (
        baseline_wallet.available_credits + 4
    )
    assert snapshot.database.wallet.reserved_credits == (
        baseline_wallet.reserved_credits + 2
    )
    assert snapshot.database.wallet.consumed_credits == (
        baseline_wallet.consumed_credits + 4
    )
    assert snapshot.database.wallet.debt_credits == baseline_wallet.debt_credits + 7
    assert snapshot.database.subscriptions == OperatorSubscriptionTotals(
        status_active=baseline_subscriptions.status_active + 1,
        entitled=baseline_subscriptions.entitled + 2,
        auto_renewing=baseline_subscriptions.auto_renewing + 1,
    )
