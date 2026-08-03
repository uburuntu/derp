"""Restart-safe refund control for the operator-only live Stars test."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Final, Protocol, Self

import logfire
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing import DEFAULT_PRODUCT_CATALOG
from derp.common.tasks import task_is_running
from derp.models import PaymentReceipt, PaymentRefundRequest, PurchaseIntent
from derp.observability import report_exception
from derp.support.types import SupportMaintenance

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

DEFAULT_DEBUG_REFUND_RECONCILIATION_INTERVAL: Final = timedelta(minutes=5)
DEFAULT_DEBUG_REFUND_UNCERTAINTY_AGE: Final = timedelta(minutes=10)
MAX_DEBUG_REFUND_BATCH_SIZE: Final = 50


class StarRefundBot(Protocol):
    """Narrow Telegram effect required by the debug refund control."""

    async def refund_star_payment(
        self,
        user_id: int,
        telegram_payment_charge_id: str,
    ) -> bool: ...


class StarRefundSettlement(Protocol):
    """Narrow local settlement effect used after provider acceptance."""

    async def clawback(self, refund: str) -> object: ...


class OperatorDebugRefundResult(StrEnum):
    """Content-free result of one exact debug-purchase refund request."""

    REQUESTED = "requested"
    PENDING = "pending"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class OperatorDebugRefundSweep:
    """Aggregate-only result of one bounded recovery pass."""

    processed: int
    reconciled: int
    pending_review: int

    @classmethod
    def empty(cls) -> Self:
        return cls(0, 0, 0)


@dataclass(frozen=True, slots=True)
class _RefundCommand:
    request_id: uuid.UUID
    user_id: int
    charge_id: str
    status: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OperatorDebugRefundService:
    """Submit exact durable refunds plus the operator debug convenience flow."""

    def __init__(
        self,
        transactions: TransactionFactory,
        bot: StarRefundBot,
        settlement: StarRefundSettlement,
        *,
        clock: Callable[[], datetime] = _utc_now,
        uncertainty_age: timedelta = DEFAULT_DEBUG_REFUND_UNCERTAINTY_AGE,
    ) -> None:
        if not callable(transactions):
            raise TypeError("transactions must be callable")
        if not callable(getattr(bot, "refund_star_payment", None)):
            raise TypeError("bot must support Star refunds")
        if not callable(getattr(settlement, "clawback", None)):
            raise TypeError("settlement must support clawback")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not timedelta(0) < uncertainty_age <= timedelta(hours=1):
            raise ValueError("uncertainty_age must be positive and at most one hour")
        self._transactions = transactions
        self._bot = bot
        self._settlement = settlement
        self._clock = clock
        self._uncertainty_age = uncertainty_age
        self._lock = asyncio.Lock()

    async def refund_latest(
        self, operator_telegram_id: int
    ) -> OperatorDebugRefundResult:
        """Create or resume one server-selected 1-Star test refund."""
        self._require_operator_id(operator_telegram_id)
        async with self._lock:
            command = await self._ensure_latest_request(operator_telegram_id)
            if command is None:
                return OperatorDebugRefundResult.NOT_FOUND
            return await self._process(command)

    async def refund_receipt(
        self,
        *,
        requester_telegram_id: int,
        payment_receipt_id: uuid.UUID,
    ) -> OperatorDebugRefundResult:
        """Create or resume a refund for one requester-owned receipt."""
        self._require_operator_id(requester_telegram_id)
        if not isinstance(payment_receipt_id, uuid.UUID):
            raise TypeError("payment_receipt_id must be a UUID")
        async with self._lock:
            command = await self._ensure_receipt_request(
                requester_telegram_id=requester_telegram_id,
                payment_receipt_id=payment_receipt_id,
            )
            if command is None:
                return OperatorDebugRefundResult.NOT_FOUND
            return await self._process(command)

    async def mark_reconciled(self, telegram_charge_id: str) -> bool:
        """Close an outbound request after the normal Telegram refund update."""
        if not isinstance(telegram_charge_id, str) or not telegram_charge_id.strip():
            raise ValueError("telegram_charge_id must not be blank")
        async with self._transactions() as session:
            request = await session.scalar(
                select(PaymentRefundRequest)
                .join(
                    PaymentReceipt,
                    PaymentReceipt.id == PaymentRefundRequest.payment_receipt_id,
                )
                .where(PaymentReceipt.telegram_charge_id == telegram_charge_id)
                .with_for_update(of=PaymentRefundRequest)
            )
            if request is None:
                return False
            self._mark_request_reconciled(request, self._aware_now())
            return True

    async def reconcile(self, *, limit: int = 20) -> OperatorDebugRefundSweep:
        """Recover safe local work and quarantine ambiguous provider calls."""
        if isinstance(limit, bool) or not 1 <= limit <= MAX_DEBUG_REFUND_BATCH_SIZE:
            raise ValueError(
                f"limit must be between 1 and {MAX_DEBUG_REFUND_BATCH_SIZE}"
            )
        async with self._lock:
            now = self._aware_now()
            async with self._transactions() as session:
                stale = list(
                    await session.scalars(
                        select(PaymentRefundRequest)
                        .where(
                            PaymentRefundRequest.status == "submitting",
                            PaymentRefundRequest.submitted_at
                            <= now - self._uncertainty_age,
                        )
                        .order_by(PaymentRefundRequest.submitted_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                for request in stale:
                    request.status = "needs_review"
                    request.last_error_code = "submission_outcome_unknown"

                commands = tuple(
                    _RefundCommand(*row)
                    for row in (
                        await session.execute(
                            select(
                                PaymentRefundRequest.id,
                                PaymentRefundRequest.requester_telegram_id,
                                PaymentReceipt.telegram_charge_id,
                                PaymentRefundRequest.status,
                            )
                            .join(
                                PaymentReceipt,
                                PaymentReceipt.id
                                == PaymentRefundRequest.payment_receipt_id,
                            )
                            .where(
                                PaymentRefundRequest.status.in_(("pending", "accepted"))
                            )
                            .order_by(PaymentRefundRequest.created_at)
                            .limit(max(0, limit - len(stale)))
                        )
                    ).tuples()
                )

            outcomes = [await self._process(command) for command in commands]
            reconciled_from_updates = await self._close_clawed_back_requests(limit)
            return OperatorDebugRefundSweep(
                processed=len(stale) + len(commands),
                reconciled=(
                    outcomes.count(OperatorDebugRefundResult.REQUESTED)
                    + reconciled_from_updates
                ),
                pending_review=(
                    len(stale) + outcomes.count(OperatorDebugRefundResult.PENDING)
                ),
            )

    async def _ensure_latest_request(
        self,
        operator_telegram_id: int,
    ) -> _RefundCommand | None:
        product = DEFAULT_PRODUCT_CATALOG.debug_top_up
        versions = tuple(
            candidate.version
            for candidate in (
                product,
                *DEFAULT_PRODUCT_CATALOG.retired_products,
            )
            if candidate.kind is product.kind
            and candidate.id == product.id
            and candidate.stars == product.stars
            and candidate.credits == product.credits
        )
        async with self._transactions() as session:
            receipt = await session.scalar(
                select(PaymentReceipt)
                .join(
                    PurchaseIntent,
                    PurchaseIntent.id == PaymentReceipt.purchase_intent_id,
                )
                .where(
                    PaymentReceipt.payer_telegram_id == operator_telegram_id,
                    PaymentReceipt.status.in_(
                        ("fulfilled", "refund_requested", "clawed_back")
                    ),
                    PaymentReceipt.currency == product.currency,
                    PaymentReceipt.total_amount == product.stars,
                    PurchaseIntent.product_kind == product.kind.value,
                    PurchaseIntent.product_id == product.id,
                    PurchaseIntent.product_version.in_(versions),
                    PurchaseIntent.credits == product.credits,
                    PurchaseIntent.stars == product.stars,
                )
                .order_by(PaymentReceipt.created_at.desc(), PaymentReceipt.id.desc())
                .with_for_update(of=PaymentReceipt)
                .limit(1)
            )
            if receipt is None:
                return None
            request = await session.scalar(
                select(PaymentRefundRequest)
                .where(PaymentRefundRequest.payment_receipt_id == receipt.id)
                .with_for_update()
            )
            if request is None:
                if receipt.status == "clawed_back":
                    return None
                request = PaymentRefundRequest(
                    payment_receipt_id=receipt.id,
                    requester_telegram_id=operator_telegram_id,
                )
                session.add(request)
                await session.flush()
                receipt.status = "refund_requested"
            elif request.status == "rejected":
                request.status = "pending"
                request.submitted_at = None
                request.last_error_code = None
                receipt.status = "refund_requested"
            return _RefundCommand(
                request.id,
                operator_telegram_id,
                receipt.telegram_charge_id,
                request.status,
            )

    async def _ensure_receipt_request(
        self,
        *,
        requester_telegram_id: int,
        payment_receipt_id: uuid.UUID,
    ) -> _RefundCommand | None:
        async with self._transactions() as session:
            receipt = await session.scalar(
                select(PaymentReceipt)
                .where(
                    PaymentReceipt.id == payment_receipt_id,
                    PaymentReceipt.payer_telegram_id == requester_telegram_id,
                    PaymentReceipt.status.in_(
                        ("fulfilled", "refund_requested", "clawed_back")
                    ),
                )
                .with_for_update()
            )
            if receipt is None:
                return None
            request = await session.scalar(
                select(PaymentRefundRequest)
                .where(PaymentRefundRequest.payment_receipt_id == receipt.id)
                .with_for_update()
            )
            if request is None:
                if receipt.status == "clawed_back":
                    return None
                request = PaymentRefundRequest(
                    payment_receipt_id=receipt.id,
                    requester_telegram_id=requester_telegram_id,
                )
                session.add(request)
                await session.flush()
                receipt.status = "refund_requested"
            elif request.status == "rejected":
                request.status = "pending"
                request.submitted_at = None
                request.last_error_code = None
                receipt.status = "refund_requested"
            return _RefundCommand(
                request.id,
                requester_telegram_id,
                receipt.telegram_charge_id,
                request.status,
            )

    async def _process(
        self,
        command: _RefundCommand,
    ) -> OperatorDebugRefundResult:
        if command.status == "reconciled":
            return OperatorDebugRefundResult.REQUESTED
        if command.status in ("submitting", "needs_review"):
            return OperatorDebugRefundResult.PENDING
        if command.status == "accepted":
            return await self._reconcile_accepted(command)
        if command.status != "pending":
            return OperatorDebugRefundResult.REJECTED

        claimed = await self._claim_pending(command.request_id)
        if claimed is None:
            return OperatorDebugRefundResult.PENDING
        try:
            accepted = await self._bot.refund_star_payment(
                user_id=claimed.user_id,
                telegram_payment_charge_id=claimed.charge_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_needs_review(claimed.request_id, type(exc).__name__)
            report_exception(
                "operator.debug_refund_submission_uncertain",
                exception=exc,
                level="warning",
            )
            return OperatorDebugRefundResult.PENDING
        if accepted is not True:
            await self._mark_rejected(claimed.request_id)
            return OperatorDebugRefundResult.REJECTED

        await self._mark_accepted(claimed.request_id)
        return await self._reconcile_accepted(
            _RefundCommand(
                claimed.request_id,
                claimed.user_id,
                claimed.charge_id,
                "accepted",
            )
        )

    async def _claim_pending(self, request_id: uuid.UUID) -> _RefundCommand | None:
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(PaymentRefundRequest, PaymentReceipt)
                    .join(
                        PaymentReceipt,
                        PaymentReceipt.id == PaymentRefundRequest.payment_receipt_id,
                    )
                    .where(PaymentRefundRequest.id == request_id)
                    .with_for_update(of=(PaymentRefundRequest, PaymentReceipt))
                )
            ).one_or_none()
            if row is None:
                return None
            request, receipt = row
            if request.status != "pending":
                return None
            request.status = "submitting"
            request.attempt_count += 1
            request.submitted_at = self._aware_now()
            request.last_error_code = None
            receipt.status = "refund_requested"
            return _RefundCommand(
                request.id,
                request.requester_telegram_id,
                receipt.telegram_charge_id,
                request.status,
            )

    async def _mark_accepted(self, request_id: uuid.UUID) -> None:
        async with self._transactions() as session:
            request = await session.get(
                PaymentRefundRequest, request_id, with_for_update=True
            )
            if request is None or request.status == "reconciled":
                return
            request.status = "accepted"
            request.accepted_at = self._aware_now()
            request.last_error_code = None

    async def _mark_rejected(self, request_id: uuid.UUID) -> None:
        async with self._transactions() as session:
            request = await session.get(
                PaymentRefundRequest, request_id, with_for_update=True
            )
            if request is None or request.status != "submitting":
                return
            receipt = await session.get(
                PaymentReceipt, request.payment_receipt_id, with_for_update=True
            )
            request.status = "rejected"
            request.last_error_code = "provider_rejected"
            if receipt is not None and receipt.status == "refund_requested":
                receipt.status = "fulfilled"

    async def _mark_needs_review(
        self,
        request_id: uuid.UUID,
        error_code: str,
    ) -> None:
        async with self._transactions() as session:
            request = await session.get(
                PaymentRefundRequest, request_id, with_for_update=True
            )
            if request is None or request.status != "submitting":
                return
            request.status = "needs_review"
            request.last_error_code = error_code[:64]

    async def _reconcile_accepted(
        self,
        command: _RefundCommand,
    ) -> OperatorDebugRefundResult:
        try:
            await self._settlement.clawback(command.charge_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report_exception(
                "operator.debug_refund_local_reconciliation_failed",
                exception=exc,
                level="warning",
            )
            return OperatorDebugRefundResult.PENDING
        async with self._transactions() as session:
            request = await session.get(
                PaymentRefundRequest, command.request_id, with_for_update=True
            )
            if request is not None:
                self._mark_request_reconciled(request, self._aware_now())
        return OperatorDebugRefundResult.REQUESTED

    async def _close_clawed_back_requests(self, limit: int) -> int:
        async with self._transactions() as session:
            requests = list(
                await session.scalars(
                    select(PaymentRefundRequest)
                    .join(
                        PaymentReceipt,
                        PaymentReceipt.id == PaymentRefundRequest.payment_receipt_id,
                    )
                    .where(
                        PaymentRefundRequest.status.in_(
                            ("accepted", "needs_review", "submitting")
                        ),
                        PaymentReceipt.status == "clawed_back",
                    )
                    .order_by(PaymentRefundRequest.created_at)
                    .limit(limit)
                    .with_for_update(of=PaymentRefundRequest, skip_locked=True)
                )
            )
            now = self._aware_now()
            for request in requests:
                self._mark_request_reconciled(request, now)
            return len(requests)

    @staticmethod
    def _mark_request_reconciled(
        request: PaymentRefundRequest,
        now: datetime,
    ) -> None:
        request.status = "reconciled"
        request.accepted_at = request.accepted_at or now
        request.reconciled_at = request.reconciled_at or now
        request.last_error_code = None

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _require_operator_id(operator_telegram_id: int) -> None:
        if (
            isinstance(operator_telegram_id, bool)
            or not isinstance(operator_telegram_id, int)
            or operator_telegram_id <= 0
        ):
            raise ValueError("operator_telegram_id must be a positive integer")


class OperatorDebugRefundWorker:
    """Run startup and periodic safe refund recovery."""

    def __init__(
        self,
        service: OperatorDebugRefundService,
        *,
        support_maintenance: SupportMaintenance | None = None,
        interval: timedelta = DEFAULT_DEBUG_REFUND_RECONCILIATION_INTERVAL,
        batch_size: int = 20,
    ) -> None:
        if not isinstance(service, OperatorDebugRefundService):
            raise TypeError("service must be an OperatorDebugRefundService")
        if not timedelta(0) < interval <= timedelta(days=1):
            raise ValueError("interval must be positive and at most one day")
        if (
            isinstance(batch_size, bool)
            or not 1 <= batch_size <= MAX_DEBUG_REFUND_BATCH_SIZE
        ):
            raise ValueError(
                f"batch_size must be between 1 and {MAX_DEBUG_REFUND_BATCH_SIZE}"
            )
        self._service = service
        self._support_maintenance = support_maintenance
        self._interval_seconds = interval.total_seconds()
        self._batch_size = batch_size
        self._sweep_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return task_is_running(self._task)

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError("debug refund worker is already running")
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_periodically(), name="operator-debug-refund-reconciliation"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> OperatorDebugRefundSweep:
        async with self._sweep_lock:
            try:
                report = await self._service.reconcile(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "operator.debug_refund_reconciliation_failed",
                    exception=exc,
                    level="warning",
                )
                return OperatorDebugRefundSweep.empty()
            support_refunds_completed = support_content_purged = 0
            support_intakes_purged = 0
            if self._support_maintenance is not None:
                try:
                    support_refunds_completed = (
                        await self._support_maintenance.complete_reconciled_refunds(
                            limit=self._batch_size
                        )
                    )
                    support_content_purged = (
                        await self._support_maintenance.purge_closed_content(
                            limit=self._batch_size
                        )
                    )
                    support_intakes_purged = (
                        await self._support_maintenance.purge_expired_intakes(
                            limit=self._batch_size
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_exception(
                        "operator.support_refund_reconciliation_failed",
                        exception=exc,
                        level="warning",
                    )
            logfire.info(
                "operator.debug_refund_reconciliation",
                processed=report.processed,
                reconciled=report.reconciled,
                pending_review=report.pending_review,
                support_refunds_completed=support_refunds_completed,
                support_content_purged=support_content_purged,
                support_intakes_purged=support_intakes_purged,
            )
            return report

    async def aclose(self) -> None:
        if (task := self._task) is None:
            return
        self._task = None
        self._stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_periodically(self) -> None:
        await self.sweep()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                await self.sweep()


__all__ = [
    "DEFAULT_DEBUG_REFUND_RECONCILIATION_INTERVAL",
    "DEFAULT_DEBUG_REFUND_UNCERTAINTY_AGE",
    "MAX_DEBUG_REFUND_BATCH_SIZE",
    "OperatorDebugRefundResult",
    "OperatorDebugRefundService",
    "OperatorDebugRefundSweep",
    "OperatorDebugRefundWorker",
    "StarRefundBot",
]
