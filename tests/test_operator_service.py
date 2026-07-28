"""Aggregate and maintenance contracts for the private operator console."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from derp.approvals.maintenance import DeferredApprovalExpiryWorker
from derp.billing import (
    PaymentUpdateReplayReport,
    PaymentUpdateReplayWorker,
    SubscriptionExpiryWorker,
    SubscriptionRenewalSweepReport,
    SubscriptionRenewalWorker,
)
from derp.catalog import OPENROUTER_MODEL_CATALOG, ModelRole
from derp.db import DatabaseManager
from derp.delivery import DeliveryMaintenanceReport, DeliveryMaintenanceWorker
from derp.history.retention import HistoryRetentionWorker
from derp.inference import (
    OpenRouterCostReconciliationReport,
    OpenRouterCostReconciliationWorker,
)
from derp.models import (
    InferenceUsage,
    Subscription,
    SubscriptionRenewalCommandRecord,
    SupportRequest,
    Wallet,
    WalletLot,
)
from derp.models import Message as MessageModel
from derp.openrouter import CurrentKeyInfo, OpenRouterClient, OpenRouterModel
from derp.operations import (
    OperationReconciliationReport,
    OperationReconciliationWorker,
)
from derp.operator import (
    OperatorActivityTotals,
    OperatorConsoleService,
    OperatorDebugRefundSweep,
    OperatorDebugRefundWorker,
    OperatorMaintenanceAction,
    OperatorProbeStatus,
    OperatorSubscriptionTotals,
    OperatorSupportTotals,
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
    openrouter_client: object | None = None,
    inference_worker: object | None = None,
) -> tuple[OperatorConsoleService, dict[OperatorMaintenanceAction, AsyncMock]]:
    sweeps = {
        OperatorMaintenanceAction.HISTORY: AsyncMock(return_value=2),
        OperatorMaintenanceAction.SUBSCRIPTIONS: AsyncMock(return_value=3),
        OperatorMaintenanceAction.PAYMENTS: AsyncMock(
            return_value=PaymentUpdateReplayReport(
                claimed_count=6,
                settled_count=4,
                attention_count=1,
                retry_scheduled_count=1,
                reply_sent_count=3,
                reply_failed_count=1,
                reply_skipped_count=1,
            )
        ),
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
    subscription_renewal_sweep = AsyncMock(
        return_value=SubscriptionRenewalSweepReport(
            claimed_count=4,
            applied_count=2,
            retry_scheduled_count=1,
            attention_count=1,
        )
    )
    sweeps[OperatorMaintenanceAction.SUBSCRIPTIONS].renewal = subscription_renewal_sweep
    subscription_renewal = SimpleNamespace(
        is_running=True,
        sweep=subscription_renewal_sweep,
    )
    debug_refund_sweep = AsyncMock(
        return_value=OperatorDebugRefundSweep(
            processed=3,
            reconciled=2,
            pending_review=1,
        )
    )
    sweeps[OperatorMaintenanceAction.PAYMENTS].refund = debug_refund_sweep
    payment_updates = SimpleNamespace(
        is_running=True,
        sweep=sweeps[OperatorMaintenanceAction.PAYMENTS],
    )
    debug_refunds = SimpleNamespace(
        is_running=True,
        sweep=debug_refund_sweep,
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
            subscription_renewal=cast(
                SubscriptionRenewalWorker,
                subscription_renewal,
            ),
            payment_update_replay=cast(PaymentUpdateReplayWorker, payment_updates),
            debug_refund_reconciliation=cast(
                OperatorDebugRefundWorker,
                debug_refunds,
            ),
            operation_reconciliation=cast(
                OperationReconciliationWorker,
                operations,
            ),
            delivery_maintenance=cast(DeliveryMaintenanceWorker, deliveries),
            approval_expiry=cast(DeferredApprovalExpiryWorker, approvals),
            inference_reconciliation=cast(
                OpenRouterCostReconciliationWorker | None,
                inference_worker,
            ),
            openrouter_client=cast(OpenRouterClient | None, openrouter_client),
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
    assert snapshot.inference is not None
    assert snapshot.inference.usage is None
    assert (
        snapshot.inference.connectivity.balance_status
        is OperatorProbeStatus.NOT_CONFIGURED
    )
    assert [worker.running for worker in snapshot.runtime.workers] == [
        True,
        True,
        True,
        False,
        True,
        False,
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


async def test_payment_runtime_status_requires_both_recovery_workers() -> None:
    service, _ = build_service(FailingDatabase())
    service._debug_refund_reconciliation.is_running = False

    snapshot = await service.snapshot()

    workers = {worker.action: worker.running for worker in snapshot.runtime.workers}
    assert workers[OperatorMaintenanceAction.PAYMENTS] is False


async def test_inference_check_reads_only_balance_and_public_catalog() -> None:
    client = MagicMock(spec=OpenRouterClient)
    client.get_current_key = AsyncMock(
        return_value=CurrentKeyInfo(
            limit=Decimal("12.50"),
            limit_remaining=Decimal("10.25"),
            usage=Decimal("2.25"),
            usage_daily=Decimal("0.25"),
            usage_weekly=Decimal("1.25"),
            usage_monthly=Decimal("2.25"),
            is_free_tier=False,
            limit_reset="monthly",
        )
    )
    visible_role = ModelRole.CHAT_STANDARD
    visible_spec = OPENROUTER_MODEL_CATALOG[visible_role]
    visible_model = OpenRouterModel.model_validate(
        {
            "id": visible_spec.provider_model_id,
            "canonical_slug": visible_spec.canonical_model_id,
            "name": visible_spec.display_name,
            "created": 1,
            "context_length": visible_spec.input_token_limit,
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "modality": "text+image->text",
            },
            "pricing": {"prompt": "0.000002", "completion": "0.00001"},
            "supported_parameters": ["tools"],
            "supported_voices": None,
        }
    )
    client.list_models = AsyncMock(
        return_value=SimpleNamespace(
            data=(visible_model,),
            total_count=321,
        )
    )
    service, _ = build_service(FailingDatabase(), openrouter_client=client)

    with patch("derp.operator.service.logfire.info") as info:
        result = await service.check_inference_connectivity(actor_id=42)

    assert result.balance_status is OperatorProbeStatus.READY
    assert result.key_remaining_usd == Decimal("10.25")
    assert result.catalog_status is OperatorProbeStatus.READY
    assert result.catalog_model_count == 321
    assert result.visible_enabled_roles == (visible_role.value,)
    client.get_current_key.assert_awaited_once_with()
    query = client.list_models.await_args.args[0]
    assert query.limit == 1000
    assert query.output_modalities == ("all",)
    client.generate_image.assert_not_awaited()
    info.assert_called_once()
    assert info.call_args.kwargs["visible_enabled_role_count"] == 1
    assert "total_credits" not in info.call_args.kwargs


async def test_inference_check_preserves_partial_connectivity_results() -> None:
    client = MagicMock(spec=OpenRouterClient)
    client.get_current_key = AsyncMock(side_effect=RuntimeError("private failure"))
    client.list_models = AsyncMock(
        return_value=SimpleNamespace(data=(), total_count=300)
    )
    service, _ = build_service(FailingDatabase(), openrouter_client=client)

    with (
        patch("derp.operator.service.report_exception") as report,
        patch("derp.operator.service.logfire.info"),
    ):
        result = await service.check_inference_connectivity(actor_id=42)

    assert result.balance_status is OperatorProbeStatus.FAILED
    assert result.catalog_status is OperatorProbeStatus.READY
    assert result.catalog_model_count == 300
    report.assert_called_once()
    assert report.call_args.args == ("operator.inference_balance_check_failed",)
    assert report.call_args.kwargs["actor_id"] == 42


@pytest.mark.parametrize(
    ("action", "expected_name", "expected_value"),
    [
        (OperatorMaintenanceAction.HISTORY, "purged_message_count", 2),
        (OperatorMaintenanceAction.SUBSCRIPTIONS, "expired_cycle_count", 3),
        (OperatorMaintenanceAction.PAYMENTS, "payment_update_claimed_count", 6),
        (OperatorMaintenanceAction.OPERATIONS, "examined_count", 3),
        (OperatorMaintenanceAction.DELIVERIES, "retry_candidate_count", 2),
        (OperatorMaintenanceAction.APPROVALS, "expired_request_count", 5),
        (OperatorMaintenanceAction.INFERENCE, "configured_count", 0),
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
    assert sweeps[OperatorMaintenanceAction.SUBSCRIPTIONS].renewal.await_count == (
        1 if action is OperatorMaintenanceAction.SUBSCRIPTIONS else 0
    )
    assert sweeps[OperatorMaintenanceAction.PAYMENTS].refund.await_count == (
        1 if action is OperatorMaintenanceAction.PAYMENTS else 0
    )
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
        OperatorMaintenanceAction.PAYMENTS,
        OperatorMaintenanceAction.OPERATIONS,
        OperatorMaintenanceAction.DELIVERIES,
        OperatorMaintenanceAction.APPROVALS,
        OperatorMaintenanceAction.INFERENCE,
    ]
    assert all(sweep.await_count == 1 for sweep in sweeps.values())
    sweeps[OperatorMaintenanceAction.SUBSCRIPTIONS].renewal.assert_awaited_once()
    sweeps[OperatorMaintenanceAction.PAYMENTS].refund.assert_awaited_once()


async def test_payment_maintenance_exposes_both_bounded_recovery_passes() -> None:
    service, sweeps = build_service()

    with patch("derp.operator.service.logfire.info"):
        result = await service.run_maintenance(
            OperatorMaintenanceAction.PAYMENTS,
            actor_id=42,
        )

    assert {item.name: item.count for item in result.passes[0].counts} == {
        "payment_update_claimed_count": 6,
        "payment_update_settled_count": 4,
        "payment_update_attention_count": 1,
        "payment_update_retry_scheduled_count": 1,
        "payment_reply_sent_count": 3,
        "payment_reply_failed_count": 1,
        "payment_reply_skipped_count": 1,
        "debug_refund_processed_count": 3,
        "debug_refund_reconciled_count": 2,
        "debug_refund_pending_review_count": 1,
    }
    sweeps[OperatorMaintenanceAction.PAYMENTS].assert_awaited_once()
    sweeps[OperatorMaintenanceAction.PAYMENTS].refund.assert_awaited_once()


async def test_inference_maintenance_exposes_bounded_reconciliation_counts() -> None:
    sweep = AsyncMock(
        return_value=OpenRouterCostReconciliationReport(
            claimed_count=3,
            reconciled_count=2,
            retry_scheduled_count=1,
            unavailable_count=0,
            claim_lost_count=0,
            stale_attempt_count=4,
        )
    )
    worker = SimpleNamespace(is_running=True, sweep=sweep)
    service, _ = build_service(inference_worker=worker)

    with patch("derp.operator.service.logfire.info"):
        result = await service.run_maintenance(
            OperatorMaintenanceAction.INFERENCE,
            actor_id=42,
        )

    counts = {item.name: item.count for item in result.passes[0].counts}
    assert counts == {
        "configured_count": 1,
        "claimed_count": 3,
        "reconciled_count": 2,
        "retry_scheduled_count": 1,
        "unavailable_count": 0,
        "claim_lost_count": 0,
        "stale_attempt_count": 4,
    }
    sweep.assert_awaited_once()


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
    baseline_support = baseline.database.support
    baseline_inference = baseline.database.inference_usage
    assert baseline_users is not None
    assert baseline_chats is not None
    assert baseline_messages is not None
    assert baseline_wallet is not None
    assert baseline_subscriptions is not None
    assert baseline_support is not None
    assert baseline_inference is not None

    recent_user = await user_factory(telegram_id=71)
    old_user = await user_factory(telegram_id=72)
    recent_chat = await chat_factory(telegram_id=-71, chat_type="group")
    old_chat = await chat_factory(telegram_id=-72, chat_type="channel")
    old_user.updated_at = now - timedelta(days=2)
    old_chat.updated_at = now - timedelta(days=2)
    recent_user.updated_at = now
    recent_chat.updated_at = now
    active_subscription = Subscription(
        user_id=recent_user.id,
        plan_id="operator-test",
        plan_version="v1",
        status="active",
        renewal_enabled=True,
        current_period_end=now + timedelta(days=10),
    )
    canceled_subscription = Subscription(
        user_id=old_user.id,
        plan_id="operator-test",
        plan_version="v1",
        status="canceled",
        renewal_enabled=False,
        current_period_end=now + timedelta(days=5),
        canceled_at=now,
    )
    db_session.add_all(
        [
            active_subscription,
            canceled_subscription,
            SupportRequest(
                reference="OPERATORS1",
                requester_user_id=recent_user.id,
                kind="payment",
                source="paysupport",
                status="open",
                created_at=now,
                updated_at=now,
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
            InferenceUsage(
                id=uuid4(),
                user_id=recent_user.id,
                chat_id=recent_chat.id,
                provider="openrouter",
                provider_model_id="reviewed-model",
                input_tokens=1_000,
                output_tokens=200,
                total_tokens=1_200,
                cache_read_tokens=300,
                cache_write_tokens=40,
                reasoning_tokens=50,
                audio_input_tokens=60,
                audio_output_tokens=70,
                usage_available=True,
                actual_cost_usd=Decimal("0.012345"),
                status="succeeded",
                reconciliation_status="reconciled",
                provider_started_at=now,
                provider_completed_at=now,
                usage_recorded_at=now,
                reconciliation_requested_at=now,
                reconciled_at=now,
            ),
            InferenceUsage(
                id=uuid4(),
                user_id=old_user.id,
                chat_id=old_chat.id,
                provider="openrouter",
                provider_model_id="reviewed-model",
                status="failed",
                reconciliation_status="pending",
                provider_started_at=now - timedelta(days=2),
                provider_completed_at=now - timedelta(days=2),
                usage_recorded_at=now - timedelta(days=2),
                reconciliation_requested_at=now - timedelta(days=2),
            ),
            InferenceUsage(
                id=uuid4(),
                user_id=recent_user.id,
                chat_id=recent_chat.id,
                provider="openrouter",
                provider_model_id="reviewed-model",
                status="pending",
                reconciliation_status="not_ready",
                provider_started_at=now,
            ),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            SubscriptionRenewalCommandRecord(
                subscription_id=active_subscription.id,
                payer_telegram_id=recent_user.telegram_id,
                telegram_charge_id="operator-attention-charge",
                desired_enabled=False,
                status="attention",
                attempt_count=8,
                next_attempt_at=now,
                last_failure_code="provider_call_failed",
            ),
            SubscriptionRenewalCommandRecord(
                subscription_id=canceled_subscription.id,
                payer_telegram_id=old_user.telegram_id,
                telegram_charge_id="operator-expired-lease-charge",
                desired_enabled=True,
                status="processing",
                attempt_count=1,
                next_attempt_at=now,
                lease_token=uuid4(),
                lease_expires_at=now - timedelta(days=1),
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
        renewal_pending=baseline_subscriptions.renewal_pending,
        renewal_processing=baseline_subscriptions.renewal_processing + 1,
        renewal_attention=baseline_subscriptions.renewal_attention + 1,
        renewal_due=baseline_subscriptions.renewal_due + 1,
    )
    assert snapshot.database.support == OperatorSupportTotals(
        open=baseline_support.open + 1,
        payment_open=baseline_support.payment_open + 1,
        resolved=baseline_support.resolved,
    )
    assert snapshot.database.inference_usage is not None
    inference = snapshot.database.inference_usage
    assert inference.attempts.total == baseline_inference.attempts.total + 3
    assert inference.attempts.recent_24h == baseline_inference.attempts.recent_24h + 2
    assert inference.attempts.succeeded == baseline_inference.attempts.succeeded + 1
    assert inference.attempts.succeeded_24h == (
        baseline_inference.attempts.succeeded_24h + 1
    )
    assert inference.attempts.failed == baseline_inference.attempts.failed + 1
    assert inference.attempts.pending == baseline_inference.attempts.pending + 1
    assert inference.tokens.input == baseline_inference.tokens.input + 1_000
    assert inference.tokens.output == baseline_inference.tokens.output + 200
    assert inference.tokens.total == baseline_inference.tokens.total + 1_200
    assert inference.tokens.cache_read == baseline_inference.tokens.cache_read + 300
    assert inference.tokens.cache_write == baseline_inference.tokens.cache_write + 40
    assert inference.tokens.reasoning == baseline_inference.tokens.reasoning + 50
    assert inference.tokens.audio_input == baseline_inference.tokens.audio_input + 60
    assert inference.tokens.audio_output == baseline_inference.tokens.audio_output + 70
    assert inference.pending_cost_reconciliation == (
        baseline_inference.pending_cost_reconciliation + 1
    )
    assert inference.unavailable_cost_count == baseline_inference.unavailable_cost_count
    assert inference.reconciled_cost_usd == (
        baseline_inference.reconciled_cost_usd + Decimal("0.012345")
    )
