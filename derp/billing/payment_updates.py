"""Crash-safe reconciliation for content-minimized Telegram payment updates."""

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
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.settlement import PaymentSettlementService
from derp.billing.types import (
    ClawbackResult,
    FulfillmentResult,
    FulfillmentState,
    PaymentConflictError,
    StoredCapturedPayment,
    StoredRefundedPayment,
)
from derp.common.tasks import task_is_running
from derp.models import PaymentUpdateInbox
from derp.observability import report_exception
from derp.support.types import SupportRefundReconciler

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

DEFAULT_PAYMENT_UPDATE_LEASE: Final = timedelta(minutes=5)
DEFAULT_PAYMENT_UPDATE_RETRY_INTERVAL: Final = timedelta(seconds=30)
DEFAULT_PAYMENT_UPDATE_REPLAY_INTERVAL: Final = timedelta(seconds=30)
MAX_PAYMENT_UPDATE_REPLAY_INTERVAL: Final = timedelta(days=1)
MAX_PAYMENT_UPDATE_ATTEMPTS: Final = 8
MAX_PAYMENT_REPLY_ATTEMPTS: Final = 8
MAX_PAYMENT_UPDATE_BATCH_SIZE: Final = 100


class Clock(Protocol):
    """Injectable aware clock for deterministic leasing and retries."""

    def __call__(self) -> datetime: ...


class PaymentUpdateKind(StrEnum):
    """Telegram service-message kinds retained by the billing inbox."""

    SUCCESSFUL = "successful_payment"
    REFUNDED = "refunded_payment"


class PaymentSettlementState(StrEnum):
    """Durable settlement fact needed to render recovery notifications."""

    FULFILLED = "fulfilled"
    CLAWED_BACK = "clawed_back"
    REFUNDED = "refunded"


class PaymentUpdateDisposition(StrEnum):
    """Stable processing outcomes without provider payload content."""

    SETTLED = "settled"
    ATTENTION = "attention"
    RETRY_SCHEDULED = "retry_scheduled"
    BUSY = "busy"
    TERMINAL = "terminal"


class PaymentReplyDisposition(StrEnum):
    """Best-effort notification result recorded after settlement."""

    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class PaymentUpdateClaimLostError(RuntimeError):
    """A payment update lease expired or was replaced before completion."""


class PaymentUpdateConflictError(RuntimeError):
    """Telegram reused one update ID with different immutable fields."""


@dataclass(frozen=True, slots=True)
class PaymentUpdateEnvelope:
    """Allowlisted, payload-hashed fields accepted by durable persistence."""

    telegram_update_id: int
    kind: PaymentUpdateKind
    payload_token_hash: str
    telegram_charge_id: str
    provider_charge_id: str | None
    payer_telegram_id: int | None
    reply_chat_id: int | None
    reply_language: str
    currency: str
    total_amount: int
    is_recurring: bool = False
    is_first_recurring: bool = False
    subscription_expiration_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.telegram_update_id, bool)
            or not isinstance(self.telegram_update_id, int)
            or self.telegram_update_id < 0
        ):
            raise ValueError("telegram_update_id must be non-negative")
        if len(self.payload_token_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.payload_token_hash
        ):
            raise ValueError("payload_token_hash must be a lowercase SHA-256 digest")
        if not self.telegram_charge_id.strip():
            raise ValueError("telegram_charge_id must not be blank")
        if self.provider_charge_id is not None and not self.provider_charge_id.strip():
            raise ValueError("provider_charge_id must not be blank when provided")
        if self.payer_telegram_id is not None and (
            isinstance(self.payer_telegram_id, bool)
            or not isinstance(self.payer_telegram_id, int)
            or self.payer_telegram_id <= 0
        ):
            raise ValueError("payer_telegram_id must be positive when provided")
        if self.reply_chat_id is not None and (
            isinstance(self.reply_chat_id, bool)
            or not isinstance(self.reply_chat_id, int)
            or self.reply_chat_id == 0
        ):
            raise ValueError("reply_chat_id must be a non-zero integer when provided")
        if self.currency != "XTR":
            raise ValueError("payment updates must use XTR")
        if (
            isinstance(self.total_amount, bool)
            or not isinstance(self.total_amount, int)
            or self.total_amount <= 0
        ):
            raise ValueError("total_amount must be positive")
        if self.reply_language not in {"en", "ru"}:
            raise ValueError("reply_language must be en or ru")
        if self.kind is PaymentUpdateKind.REFUNDED and (
            self.is_recurring
            or self.is_first_recurring
            or self.subscription_expiration_at is not None
        ):
            raise ValueError("refund updates cannot carry subscription fields")
        if self.is_first_recurring and not self.is_recurring:
            raise ValueError("a first recurring payment must be recurring")
        if self.subscription_expiration_at is not None:
            _require_aware(
                self.subscription_expiration_at, "subscription_expiration_at"
            )
            if not self.is_recurring:
                raise ValueError("subscription expiration requires a recurring payment")


@dataclass(frozen=True, slots=True)
class PaymentUpdateOutcome:
    """One claimed update outcome plus its notification lease."""

    inbox_id: uuid.UUID
    kind: PaymentUpdateKind
    disposition: PaymentUpdateDisposition
    reply_chat_id: int | None = None
    reply_language: str = "en"
    lease_token: uuid.UUID | None = None
    fulfillment: FulfillmentResult | None = None
    clawback: ClawbackResult | None = None
    settlement_state: PaymentSettlementState | None = None
    attention_reason: str | None = None

    @property
    def needs_notification(self) -> bool:
        return self.disposition in {
            PaymentUpdateDisposition.SETTLED,
            PaymentUpdateDisposition.ATTENTION,
        }


@dataclass(frozen=True, slots=True)
class PaymentUpdateReplayReport:
    """Identifier-free aggregate from one bounded replay sweep."""

    claimed_count: int = 0
    settled_count: int = 0
    attention_count: int = 0
    retry_scheduled_count: int = 0
    reply_sent_count: int = 0
    reply_failed_count: int = 0
    reply_skipped_count: int = 0


@dataclass(frozen=True, slots=True)
class _PaymentUpdateClaim:
    inbox_id: uuid.UUID
    lease_token: uuid.UUID
    kind: PaymentUpdateKind
    payload_token_hash: str
    telegram_charge_id: str
    provider_charge_id: str | None
    payer_telegram_id: int | None
    reply_chat_id: int | None
    reply_language: str
    currency: str
    total_amount: int
    is_recurring: bool
    is_first_recurring: bool
    subscription_expiration_at: datetime | None
    attempt_count: int
    reply_attempt_count: int
    settled_at: datetime | None
    settlement_state: str | None
    attention_reason: str | None


class PaymentUpdateNotifier(Protocol):
    """Send one content-minimized post-settlement notification."""

    async def notify(
        self, outcome: PaymentUpdateOutcome
    ) -> PaymentReplyDisposition: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PaymentUpdateInboxService:
    """Persist, lease, and reconcile Telegram Stars updates exactly once locally."""

    def __init__(
        self,
        transactions: TransactionFactory,
        settlement: PaymentSettlementService,
        *,
        clock: Clock = _utc_now,
        lease_duration: timedelta = DEFAULT_PAYMENT_UPDATE_LEASE,
        retry_interval: timedelta = DEFAULT_PAYMENT_UPDATE_RETRY_INTERVAL,
        max_attempts: int = MAX_PAYMENT_UPDATE_ATTEMPTS,
        max_reply_attempts: int = MAX_PAYMENT_REPLY_ATTEMPTS,
    ) -> None:
        if (
            not isinstance(lease_duration, timedelta)
            or lease_duration <= timedelta(0)
            or lease_duration > timedelta(hours=1)
        ):
            raise ValueError("lease_duration must be positive and at most one hour")
        if (
            not isinstance(retry_interval, timedelta)
            or retry_interval <= timedelta(0)
            or retry_interval > timedelta(hours=1)
        ):
            raise ValueError("retry_interval must be positive and at most one hour")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 32
        ):
            raise ValueError("max_attempts must be between 1 and 32")
        if (
            isinstance(max_reply_attempts, bool)
            or not isinstance(max_reply_attempts, int)
            or not 1 <= max_reply_attempts <= 32
        ):
            raise ValueError("max_reply_attempts must be between 1 and 32")
        self._transactions = transactions
        self._settlement = settlement
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_interval = retry_interval
        self._max_attempts = max_attempts
        self._max_reply_attempts = max_reply_attempts

    async def persist(self, envelope: PaymentUpdateEnvelope) -> uuid.UUID:
        """Commit one update before the long-poll offset may advance."""
        now = self._aware_now()
        async with self._transactions() as session:
            inbox_id = await session.scalar(
                insert(PaymentUpdateInbox)
                .values(
                    telegram_update_id=envelope.telegram_update_id,
                    kind=envelope.kind.value,
                    payload_token_hash=envelope.payload_token_hash,
                    telegram_charge_id=envelope.telegram_charge_id,
                    provider_charge_id=envelope.provider_charge_id,
                    payer_telegram_id=envelope.payer_telegram_id,
                    reply_chat_id=envelope.reply_chat_id,
                    reply_language=envelope.reply_language,
                    currency=envelope.currency,
                    total_amount=envelope.total_amount,
                    is_recurring=envelope.is_recurring,
                    is_first_recurring=envelope.is_first_recurring,
                    subscription_expiration_at=envelope.subscription_expiration_at,
                    status="pending",
                    attempt_count=0,
                    reply_attempt_count=0,
                    next_attempt_at=now,
                    reply_status="pending",
                )
                .on_conflict_do_nothing(
                    index_elements=[PaymentUpdateInbox.telegram_update_id]
                )
                .returning(PaymentUpdateInbox.id)
            )
            if inbox_id is not None:
                return inbox_id
            existing = await session.scalar(
                select(PaymentUpdateInbox).where(
                    PaymentUpdateInbox.telegram_update_id == envelope.telegram_update_id
                )
            )
            if existing is None:  # pragma: no cover - unique-index contract
                raise PaymentUpdateConflictError("payment update conflict disappeared")
            self._assert_matches(existing, envelope)
            return existing.id

    async def reconcile(self, inbox_id: uuid.UUID) -> PaymentUpdateOutcome:
        """Claim and process one live update without spanning provider I/O."""
        claim, terminal = await self._claim_one(inbox_id)
        if claim is None:
            return PaymentUpdateOutcome(
                inbox_id,
                PaymentUpdateKind.SUCCESSFUL,
                terminal,
            )
        return await self.process_claim(claim)

    async def claim_due(self, *, limit: int = 20) -> tuple[_PaymentUpdateClaim, ...]:
        """Lease a bounded, update-ordered replay batch."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_PAYMENT_UPDATE_BATCH_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_PAYMENT_UPDATE_BATCH_SIZE}"
            )
        now = self._aware_now()
        async with self._transactions() as session:
            rows = list(
                await session.scalars(
                    select(PaymentUpdateInbox)
                    .where(
                        or_(
                            (
                                (PaymentUpdateInbox.status == "pending")
                                & (PaymentUpdateInbox.next_attempt_at <= now)
                            ),
                            (
                                (PaymentUpdateInbox.status == "processing")
                                & (PaymentUpdateInbox.lease_expires_at <= now)
                            ),
                        )
                    )
                    .order_by(PaymentUpdateInbox.telegram_update_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            return tuple(self._lease(row, now) for row in rows)

    async def process_claim(
        self,
        claim: _PaymentUpdateClaim,
    ) -> PaymentUpdateOutcome:
        """Settle one lease; unexpected failures become bounded retries."""
        if claim.attention_reason is not None:
            return self._outcome(
                claim,
                PaymentUpdateDisposition.ATTENTION,
                attention_reason=claim.attention_reason,
            )
        if claim.settled_at is not None:
            return self._outcome(claim, PaymentUpdateDisposition.SETTLED)

        try:
            if claim.kind is PaymentUpdateKind.SUCCESSFUL:
                if claim.payer_telegram_id is None or claim.provider_charge_id is None:
                    return await self._attention(claim, "missing_payment_identity")
                fulfillment = await self._settlement.fulfill(
                    StoredCapturedPayment(
                        payload_token_hash=claim.payload_token_hash,
                        telegram_charge_id=claim.telegram_charge_id,
                        provider_charge_id=claim.provider_charge_id,
                        payer_telegram_id=claim.payer_telegram_id,
                        currency=claim.currency,
                        total_amount=claim.total_amount,
                        is_recurring=claim.is_recurring,
                        is_first_recurring=claim.is_first_recurring,
                        subscription_expiration_at=claim.subscription_expiration_at,
                    )
                )
                if fulfillment.state is FulfillmentState.NEEDS_REVIEW:
                    return await self._attention(claim, "settlement_needs_review")
                settlement_state = PaymentSettlementState(fulfillment.state.value)
                await self._mark_settled(claim, settlement_state)
                return self._outcome(
                    claim,
                    PaymentUpdateDisposition.SETTLED,
                    fulfillment=fulfillment,
                    settlement_state=settlement_state,
                )

            clawback = await self._settlement.clawback(
                StoredRefundedPayment(
                    payload_token_hash=claim.payload_token_hash,
                    telegram_charge_id=claim.telegram_charge_id,
                    provider_charge_id=claim.provider_charge_id,
                    currency=claim.currency,
                    total_amount=claim.total_amount,
                )
            )
            await self._mark_settled(claim, PaymentSettlementState.REFUNDED)
            return self._outcome(
                claim,
                PaymentUpdateDisposition.SETTLED,
                clawback=clawback,
                settlement_state=PaymentSettlementState.REFUNDED,
            )
        except asyncio.CancelledError:
            raise
        except LookupError:
            return await self._retry_or_attention(claim)
        except PaymentConflictError, ValueError:
            return await self._attention(claim, "settlement_conflict")
        except Exception:
            return await self._retry_or_attention(claim)

    async def finish_notification(
        self,
        outcome: PaymentUpdateOutcome,
        reply: PaymentReplyDisposition,
    ) -> None:
        """Persist one reply attempt and schedule bounded delivery recovery."""
        if not outcome.needs_notification or outcome.lease_token is None:
            raise ValueError("outcome does not hold a notification lease")
        if not isinstance(reply, PaymentReplyDisposition):
            raise TypeError("reply must be a PaymentReplyDisposition")
        now = self._aware_now()
        async with self._transactions() as session:
            row = await session.scalar(
                select(PaymentUpdateInbox)
                .where(
                    PaymentUpdateInbox.id == outcome.inbox_id,
                    PaymentUpdateInbox.status == "processing",
                    PaymentUpdateInbox.lease_token == outcome.lease_token,
                )
                .with_for_update()
            )
            if row is None:
                raise PaymentUpdateClaimLostError("payment update lease was lost")
            row.reply_attempt_count += 1
            row.reply_status = reply.value
            row.replied_at = None
            if reply is PaymentReplyDisposition.SENT:
                row.status = "attention" if row.attention_reason else "completed"
                row.replied_at = now
                row.last_failure_code = row.attention_reason
            elif reply is PaymentReplyDisposition.SKIPPED:
                reason = "reply_destination_unavailable"
                row.status = "attention"
                row.attention_reason = row.attention_reason or reason
                row.last_failure_code = reason
            elif row.reply_attempt_count >= self._max_reply_attempts:
                reason = "reply_attempts_exhausted"
                row.status = "attention"
                row.attention_reason = row.attention_reason or reason
                row.last_failure_code = reason
            else:
                multiplier = 2 ** max(row.reply_attempt_count - 1, 0)
                row.status = "pending"
                row.next_attempt_at = now + min(
                    self._retry_interval * multiplier,
                    timedelta(minutes=30),
                )
                row.last_failure_code = "reply_delivery_failed"
            row.lease_token = None
            row.lease_expires_at = None

    async def _claim_one(
        self,
        inbox_id: uuid.UUID,
    ) -> tuple[_PaymentUpdateClaim | None, PaymentUpdateDisposition]:
        now = self._aware_now()
        async with self._transactions() as session:
            row = await session.scalar(
                select(PaymentUpdateInbox)
                .where(PaymentUpdateInbox.id == inbox_id)
                .with_for_update()
            )
            if row is None:
                return None, PaymentUpdateDisposition.TERMINAL
            if row.status in {"completed", "attention"}:
                return None, PaymentUpdateDisposition.TERMINAL
            if row.status == "processing" and row.lease_expires_at > now:
                return None, PaymentUpdateDisposition.BUSY
            if row.status == "pending" and row.next_attempt_at > now:
                return None, PaymentUpdateDisposition.BUSY
            return self._lease(row, now), PaymentUpdateDisposition.BUSY

    def _lease(self, row: PaymentUpdateInbox, now: datetime) -> _PaymentUpdateClaim:
        continuing = row.settled_at is not None or row.attention_reason is not None
        if not continuing:
            row.attempt_count += 1
        row.status = "processing"
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = now + self._lease_duration
        return self._claim_from_row(row)

    async def _mark_settled(
        self,
        claim: _PaymentUpdateClaim,
        settlement_state: PaymentSettlementState,
    ) -> None:
        now = self._aware_now()
        async with self._transactions() as session:
            row = await self._locked_claim(session, claim)
            row.settled_at = now
            row.settlement_state = settlement_state.value
            row.last_failure_code = None

    async def _attention(
        self,
        claim: _PaymentUpdateClaim,
        reason: str,
    ) -> PaymentUpdateOutcome:
        async with self._transactions() as session:
            row = await self._locked_claim(session, claim)
            row.attention_reason = reason
            row.last_failure_code = reason
        return self._outcome(
            claim,
            PaymentUpdateDisposition.ATTENTION,
            attention_reason=reason,
        )

    async def _retry_or_attention(
        self,
        claim: _PaymentUpdateClaim,
    ) -> PaymentUpdateOutcome:
        if claim.attempt_count >= self._max_attempts:
            return await self._attention(claim, "settlement_attempts_exhausted")
        now = self._aware_now()
        multiplier = 2 ** max(claim.attempt_count - 1, 0)
        delay = min(self._retry_interval * multiplier, timedelta(minutes=30))
        async with self._transactions() as session:
            row = await self._locked_claim(session, claim)
            row.status = "pending"
            row.next_attempt_at = now + delay
            row.last_failure_code = "transient_settlement_failure"
            row.lease_token = None
            row.lease_expires_at = None
        return self._outcome(claim, PaymentUpdateDisposition.RETRY_SCHEDULED)

    @staticmethod
    async def _locked_claim(
        session: AsyncSession,
        claim: _PaymentUpdateClaim,
    ) -> PaymentUpdateInbox:
        row = await session.scalar(
            select(PaymentUpdateInbox)
            .where(
                PaymentUpdateInbox.id == claim.inbox_id,
                PaymentUpdateInbox.status == "processing",
                PaymentUpdateInbox.lease_token == claim.lease_token,
            )
            .with_for_update()
        )
        if row is None:
            raise PaymentUpdateClaimLostError("payment update lease was lost")
        return row

    @staticmethod
    def _claim_from_row(row: PaymentUpdateInbox) -> _PaymentUpdateClaim:
        if row.lease_token is None:  # pragma: no cover - leasing invariant
            raise RuntimeError("payment update lease token is missing")
        return _PaymentUpdateClaim(
            inbox_id=row.id,
            lease_token=row.lease_token,
            kind=PaymentUpdateKind(row.kind),
            payload_token_hash=row.payload_token_hash,
            telegram_charge_id=row.telegram_charge_id,
            provider_charge_id=row.provider_charge_id,
            payer_telegram_id=row.payer_telegram_id,
            reply_chat_id=row.reply_chat_id,
            reply_language=row.reply_language,
            currency=row.currency,
            total_amount=row.total_amount,
            is_recurring=row.is_recurring,
            is_first_recurring=row.is_first_recurring,
            subscription_expiration_at=row.subscription_expiration_at,
            attempt_count=row.attempt_count,
            reply_attempt_count=row.reply_attempt_count,
            settled_at=row.settled_at,
            settlement_state=row.settlement_state,
            attention_reason=row.attention_reason,
        )

    @staticmethod
    def _outcome(
        claim: _PaymentUpdateClaim,
        disposition: PaymentUpdateDisposition,
        *,
        fulfillment: FulfillmentResult | None = None,
        clawback: ClawbackResult | None = None,
        settlement_state: PaymentSettlementState | None = None,
        attention_reason: str | None = None,
    ) -> PaymentUpdateOutcome:
        durable_state = (
            PaymentSettlementState(claim.settlement_state)
            if settlement_state is None and claim.settlement_state is not None
            else settlement_state
        )
        return PaymentUpdateOutcome(
            inbox_id=claim.inbox_id,
            kind=claim.kind,
            disposition=disposition,
            reply_chat_id=claim.reply_chat_id,
            reply_language=claim.reply_language,
            lease_token=claim.lease_token,
            fulfillment=fulfillment,
            clawback=clawback,
            settlement_state=durable_state,
            attention_reason=attention_reason,
        )

    @staticmethod
    def _assert_matches(
        row: PaymentUpdateInbox,
        envelope: PaymentUpdateEnvelope,
    ) -> None:
        actual = (
            row.kind,
            row.payload_token_hash,
            row.telegram_charge_id,
            row.provider_charge_id,
            row.payer_telegram_id,
            row.reply_chat_id,
            row.reply_language,
            row.currency,
            row.total_amount,
            row.is_recurring,
            row.is_first_recurring,
            row.subscription_expiration_at,
        )
        expected = (
            envelope.kind.value,
            envelope.payload_token_hash,
            envelope.telegram_charge_id,
            envelope.provider_charge_id,
            envelope.payer_telegram_id,
            envelope.reply_chat_id,
            envelope.reply_language,
            envelope.currency,
            envelope.total_amount,
            envelope.is_recurring,
            envelope.is_first_recurring,
            envelope.subscription_expiration_at,
        )
        if actual != expected:
            raise PaymentUpdateConflictError(
                "Telegram update ID was replayed with different payment fields"
            )

    def _aware_now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now


class PaymentUpdateReplayWorker:
    """Replay expired payment leases and pending updates in bounded batches."""

    def __init__(
        self,
        service: PaymentUpdateInboxService,
        notifier: PaymentUpdateNotifier,
        *,
        support_refunds: SupportRefundReconciler | None = None,
        interval: timedelta = DEFAULT_PAYMENT_UPDATE_REPLAY_INTERVAL,
        batch_size: int = 20,
    ) -> None:
        if (
            not isinstance(interval, timedelta)
            or interval <= timedelta(0)
            or interval > MAX_PAYMENT_UPDATE_REPLAY_INTERVAL
        ):
            raise ValueError("interval must be positive and at most one day")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= MAX_PAYMENT_UPDATE_BATCH_SIZE
        ):
            raise ValueError(
                f"batch_size must be between 1 and {MAX_PAYMENT_UPDATE_BATCH_SIZE}"
            )
        self._service = service
        self._notifier = notifier
        self._support_refunds = support_refunds
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
            raise RuntimeError("Payment update replay worker is already running")
        self._stop.clear()
        await self.sweep()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="payment-update-replay",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> PaymentUpdateReplayReport:
        """Reconcile and notify one serialized batch."""
        async with self._sweep_lock:
            try:
                claims = await self._service.claim_due(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "payment_update_claim_failed",
                    exception=exc,
                    level="warning",
                )
                return PaymentUpdateReplayReport()

            settled = attention = retry = sent = failed = skipped = 0
            support_refunds_completed = 0
            for claim in claims:
                try:
                    outcome = await self._service.process_claim(claim)
                    if outcome.disposition is PaymentUpdateDisposition.SETTLED:
                        settled += 1
                    elif outcome.disposition is PaymentUpdateDisposition.ATTENTION:
                        attention += 1
                    elif (
                        outcome.disposition is PaymentUpdateDisposition.RETRY_SCHEDULED
                    ):
                        retry += 1
                    if not outcome.needs_notification:
                        continue
                    reply = await self._notifier.notify(outcome)
                    await self._service.finish_notification(outcome, reply)
                    sent += reply is PaymentReplyDisposition.SENT
                    failed += reply is PaymentReplyDisposition.FAILED
                    skipped += reply is PaymentReplyDisposition.SKIPPED
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_exception(
                        "payment_update_replay_failed",
                        exception=exc,
                        level="warning",
                    )
            if self._support_refunds is not None:
                try:
                    support_refunds_completed = (
                        await self._support_refunds.complete_reconciled_refunds(
                            limit=self._batch_size
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_exception(
                        "support_refund_reconciliation_failed",
                        exception=exc,
                        level="warning",
                    )

        report = PaymentUpdateReplayReport(
            claimed_count=len(claims),
            settled_count=settled,
            attention_count=attention,
            retry_scheduled_count=retry,
            reply_sent_count=sent,
            reply_failed_count=failed,
            reply_skipped_count=skipped,
        )
        if support_refunds_completed:
            logfire.info(
                "support_refunds_reconciled",
                completed_count=support_refunds_completed,
            )
        if report.claimed_count:
            logfire.info(
                "payment_update_replay_sweep",
                claimed_count=report.claimed_count,
                settled_count=report.settled_count,
                attention_count=report.attention_count,
                retry_scheduled_count=report.retry_scheduled_count,
                reply_sent_count=report.reply_sent_count,
                reply_failed_count=report.reply_failed_count,
                reply_skipped_count=report.reply_skipped_count,
            )
        return report

    async def aclose(self) -> None:
        if (task := self._task) is None:
            return
        self._task = None
        self._stop.set()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_periodically(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                await self.sweep()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "DEFAULT_PAYMENT_UPDATE_LEASE",
    "DEFAULT_PAYMENT_UPDATE_REPLAY_INTERVAL",
    "MAX_PAYMENT_UPDATE_ATTEMPTS",
    "MAX_PAYMENT_REPLY_ATTEMPTS",
    "PaymentReplyDisposition",
    "PaymentSettlementState",
    "PaymentUpdateClaimLostError",
    "PaymentUpdateConflictError",
    "PaymentUpdateDisposition",
    "PaymentUpdateEnvelope",
    "PaymentUpdateInboxService",
    "PaymentUpdateKind",
    "PaymentUpdateNotifier",
    "PaymentUpdateOutcome",
    "PaymentUpdateReplayReport",
    "PaymentUpdateReplayWorker",
]
