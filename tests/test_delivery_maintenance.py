"""Lifecycle and privacy contracts for durable delivery maintenance."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from derp.delivery import (
    ArtifactCleanup,
    Delivered,
    DeliveryFailed,
    DeliveryMaintenanceReport,
    DeliveryMaintenanceWorker,
    DeliveryReconciliation,
    DeliveryUncertain,
)
from derp.operations import OperationId


class IdleDelivery:
    """Record complete maintenance cycles without performing I/O."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.completed = asyncio.Event()

    async def reconcile_interrupted(self, **_kwargs) -> DeliveryReconciliation:
        self.calls.append("interrupted")
        return DeliveryReconciliation(())

    async def pending_operation_ids(self, **_kwargs) -> tuple[OperationId, ...]:
        self.calls.append("pending")
        return ()

    async def deliver(self, _operation_id: OperationId):
        raise AssertionError("idle maintenance must not deliver")

    async def reconcile_expired(self, **_kwargs) -> DeliveryReconciliation:
        self.calls.append("expired")
        return DeliveryReconciliation(())

    async def cleanup_expired_artifacts(self, **_kwargs) -> ArtifactCleanup:
        self.calls.append("cleanup")
        self.completed.set()
        return ArtifactCleanup(0, 0, 0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"interval": timedelta(0)}, "interval"),
        ({"stale_after": timedelta(seconds=59)}, "stale_after"),
        ({"batch_size": 0}, "batch_size"),
        ({"batch_size": 1_001}, "batch_size"),
        ({"concurrency": 0}, "concurrency"),
        ({"concurrency": 33}, "concurrency"),
    ],
)
def test_worker_rejects_unbounded_configuration(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DeliveryMaintenanceWorker(IdleDelivery(), **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_worker_sweeps_at_startup_then_periodically_and_stops() -> None:
    delivery = IdleDelivery()
    worker = DeliveryMaintenanceWorker(
        delivery,
        interval=timedelta(milliseconds=10),
    )

    async with worker:
        assert worker.is_running
        assert delivery.calls == ["interrupted", "pending", "expired", "cleanup"]
        delivery.completed.clear()
        await asyncio.wait_for(delivery.completed.wait(), timeout=1)

    calls_after_close = len(delivery.calls)
    assert not worker.is_running
    await asyncio.sleep(0.02)
    assert len(delivery.calls) == calls_after_close == 8
    await worker.aclose()


@pytest.mark.asyncio
async def test_worker_bounds_retries_isolates_operations_and_logs_counts_only() -> None:
    operation_ids = tuple(OperationId(uuid4()) for _ in range(5))
    now = datetime(2026, 7, 21, tzinfo=UTC)
    private_error = RuntimeError("private provider detail")

    class MixedDelivery(IdleDelivery):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.limits: list[int] = []

        async def reconcile_interrupted(self, **kwargs) -> DeliveryReconciliation:
            self.limits.append(kwargs["limit"])
            return DeliveryReconciliation((OperationId(uuid4()),))

        async def pending_operation_ids(self, **kwargs) -> tuple[OperationId, ...]:
            self.limits.append(kwargs["limit"])
            return operation_ids

        async def deliver(self, operation_id: OperationId):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                index = operation_ids.index(operation_id)
                if index == 0:
                    return Delivered((100,))
                if index == 1:
                    return DeliveryFailed("retry_later", retryable=True)
                if index == 2:
                    return DeliveryFailed("rejected", retryable=False)
                if index == 3:
                    return DeliveryUncertain("timeout")
                raise private_error
            finally:
                self.active -= 1

        async def reconcile_expired(self, **kwargs) -> DeliveryReconciliation:
            self.limits.append(kwargs["limit"])
            return DeliveryReconciliation((OperationId(uuid4()),), failed_count=1)

        async def cleanup_expired_artifacts(self, **kwargs) -> ArtifactCleanup:
            self.limits.append(kwargs["limit"])
            return ArtifactCleanup(3, 2, 1)

    delivery = MixedDelivery()
    worker = DeliveryMaintenanceWorker(
        delivery,
        batch_size=5,
        concurrency=2,
        clock=lambda: now,
    )

    with (
        patch("derp.delivery.maintenance.logfire.info") as info,
        patch("derp.delivery.maintenance.logfire.warning") as warning,
        patch("derp.delivery.maintenance.report_exception") as report_exception,
    ):
        report = await worker.sweep()

    assert report == DeliveryMaintenanceReport(
        interrupted_count=1,
        retry_candidate_count=5,
        delivered_count=1,
        retryable_failure_count=1,
        terminal_failure_count=1,
        uncertain_count=1,
        retry_exception_count=1,
        expired_count=1,
        expiration_failure_count=1,
        artifact_examined_count=3,
        artifact_purged_count=2,
        artifact_failure_count=1,
    )
    assert delivery.max_active == 2
    assert delivery.limits == [5, 5, 5, 5]
    assert info.call_args.kwargs["retry_candidate_count"] == 5
    assert warning.call_args.kwargs == {
        "operation_failure_count": 2,
        "artifact_failure_count": 1,
        "phase_failure_count": 0,
    }
    report_exception.assert_called_once_with(
        "delivery_maintenance_operation_failed",
        exception=private_error,
        level="warning",
        phase="pending_retry",
        failure_count=1,
    )
    assert "private provider detail" not in repr(info.call_args_list)
    assert "private provider detail" not in repr(warning.call_args_list)
    assert all(
        str(operation_id) not in repr(info.call_args_list)
        for operation_id in operation_ids
    )


@pytest.mark.asyncio
async def test_worker_continues_after_each_failed_phase() -> None:
    errors = {
        "interrupted_reconciliation": RuntimeError("interrupted secret"),
        "pending_discovery": RuntimeError("pending secret"),
        "expiration_reconciliation": RuntimeError("expiry secret"),
        "artifact_cleanup": RuntimeError("cleanup secret"),
    }

    class BrokenDelivery(IdleDelivery):
        async def reconcile_interrupted(self, **_kwargs) -> DeliveryReconciliation:
            raise errors["interrupted_reconciliation"]

        async def pending_operation_ids(self, **_kwargs) -> tuple[OperationId, ...]:
            raise errors["pending_discovery"]

        async def reconcile_expired(self, **_kwargs) -> DeliveryReconciliation:
            raise errors["expiration_reconciliation"]

        async def cleanup_expired_artifacts(self, **_kwargs) -> ArtifactCleanup:
            raise errors["artifact_cleanup"]

    with (
        patch("derp.delivery.maintenance.logfire.warning") as warning,
        patch("derp.delivery.maintenance.report_exception") as report_exception,
    ):
        report = await DeliveryMaintenanceWorker(BrokenDelivery()).sweep()

    assert report == DeliveryMaintenanceReport(phase_failure_count=4)
    assert warning.call_args.kwargs == {
        "operation_failure_count": 0,
        "artifact_failure_count": 0,
        "phase_failure_count": 4,
    }
    assert "secret" not in repr(warning.call_args_list)
    assert report_exception.call_count == 4
    assert [call.kwargs["phase"] for call in report_exception.call_args_list] == list(
        errors
    )
    assert all(
        call.kwargs["failure_count"] == 1 for call in report_exception.call_args_list
    )
    safe_calls = [
        (
            call.args,
            {key: value for key, value in call.kwargs.items() if key != "exception"},
        )
        for call in report_exception.call_args_list
    ]
    assert "secret" not in repr(safe_calls)


def test_report_rejects_inconsistent_counts() -> None:
    with pytest.raises(ValueError, match="retry outcomes"):
        DeliveryMaintenanceReport(retry_candidate_count=1)
