"""Atomic wallet selection, reservation, and settlement."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

import logfire
from sqlalchemy import case, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.catalog import InferenceProvider, ModelRole
from derp.execution import Feature
from derp.models import (
    Artifact,
    Chat,
    DeliveryIntent,
    OperationAllocation,
    OperationQuote,
    PaidOperation,
    PaymentReceipt,
    PersonalSpendConsent,
    Subscription,
    SubscriptionCycle,
    Wallet,
    WalletLedgerEntry,
    WalletLot,
)
from derp.operations.debt import DebtRecovery, WalletDebtProvenance
from derp.operations.types import (
    ContextBand,
    DeliveryState,
    FundingAuthorization,
    InventoryAllocation,
    InventoryKind,
    OperationId,
    OperationSnapshot,
    OperationState,
    Quote,
    QuoteId,
    QuoteKey,
    ReservationRejected,
    ReservationRejection,
    ReservationResult,
    ReservedOperation,
    SettlementResult,
    WalletActivity,
    WalletActivityKind,
    WalletBalance,
    WalletOwner,
    WalletOwnerKind,
    WalletStatement,
)

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Clock(Protocol):
    """Injectable UTC clock for expiry-sensitive transitions."""

    def __call__(self) -> datetime: ...


class OperationLedgerError(RuntimeError):
    """Base error for violated operation invariants."""


class ImmutableQuoteConflictError(OperationLedgerError):
    """A stable request key was retried with different commercial terms."""


class InvalidOperationTransitionError(OperationLedgerError):
    """A caller attempted a transition outside the operation state machine."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


_ACTIVITY_KIND_BY_EVENT: Mapping[str, WalletActivityKind] = MappingProxyType(
    {
        "capture": WalletActivityKind.CHARGE,
        "reversal": WalletActivityKind.REFUND,
        "clawback": WalletActivityKind.PAYMENT_CLAWBACK,
        "debt_incurred": WalletActivityKind.DEBT_INCURRED,
    }
)


class OperationLedger:
    """Own short database units of work for all wallet state transitions."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._clock = clock

    async def register_quote(
        self,
        quote: Quote,
        *,
        request_key: str,
        requester_id: uuid.UUID,
        chat_id: uuid.UUID,
        thread_id: int | None,
        pricing_input: Mapping[str, object],
    ) -> OperationId:
        """Persist a quote once, preserving the original operation-ID API."""
        stored = await self.ensure_quote(
            quote,
            request_key=request_key,
            requester_id=requester_id,
            chat_id=chat_id,
            thread_id=thread_id,
            pricing_input=pricing_input,
        )
        return stored.operation_id

    async def ensure_quote(
        self,
        quote: Quote,
        *,
        request_key: str,
        requester_id: uuid.UUID,
        chat_id: uuid.UUID,
        thread_id: int | None,
        pricing_input: Mapping[str, object],
    ) -> Quote:
        """Create a quote or return the original quote for an equivalent retry.

        Quote IDs and timestamps are attempt-local inputs. The operation and request
        identities, ownership, scope, and commercial terms must remain identical.
        """
        if not request_key.strip():
            raise ValueError("request_key must not be blank")
        if thread_id is not None and thread_id <= 0:
            raise ValueError("thread_id must be positive")

        quote_values = {
            "id": quote.id.value,
            "operation_id": quote.operation_id.value,
            "request_key": request_key,
            "requester_id": requester_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "feature": quote.key.feature.value,
            "model_key": quote.key.model_key.value,
            "provider": quote.provider.value,
            "provider_model_id": quote.provider_model_id,
            "context_band": quote.key.context_band.value,
            "variant": quote.key.variant,
            "amount_credits": quote.credits,
            "estimated_provider_cost_usd": quote.estimated_provider_cost_usd,
            "pricing_version": quote.pricing_version,
            "catalog_verified_on": quote.catalog_verified_on,
            "pricing_input": dict(pricing_input),
            "expires_at": quote.expires_at,
            "created_at": quote.created_at,
        }
        async with self._transactions() as session:
            created_quote_id = await session.scalar(
                insert(OperationQuote)
                .values(**quote_values)
                .on_conflict_do_nothing()
                .returning(OperationQuote.id)
            )
            if created_quote_id is None:
                stored_quote = await self._quote_for_retry(
                    session,
                    operation_id=quote.operation_id,
                    request_key=request_key,
                )
                if stored_quote is None or not self._quote_matches(
                    stored_quote, quote_values
                ):
                    raise ImmutableQuoteConflictError(
                        f"Conflicting quote for operation {quote.operation_id}"
                    )
            else:
                stored_quote = await session.get(OperationQuote, created_quote_id)
                if stored_quote is None:
                    raise OperationLedgerError("Created operation quote disappeared")

            created_operation_id = await session.scalar(
                insert(PaidOperation)
                .values(
                    id=quote.operation_id.value,
                    quote_id=stored_quote.id,
                    state=OperationState.QUOTED.value,
                )
                .on_conflict_do_nothing(index_elements=[PaidOperation.id])
                .returning(PaidOperation.id)
            )
            if created_operation_id is None:
                operation = await session.get(PaidOperation, quote.operation_id.value)
                if operation is None or operation.quote_id != stored_quote.id:
                    raise ImmutableQuoteConflictError(
                        f"Conflicting operation record for {quote.operation_id}"
                    )
            return self._domain_quote(stored_quote)

    async def get_snapshot(self, operation_id: OperationId) -> OperationSnapshot:
        """Load the durable facts needed to resume without repeating provider work."""
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(PaidOperation, OperationQuote)
                    .join(
                        OperationQuote,
                        OperationQuote.id == PaidOperation.quote_id,
                    )
                    .where(PaidOperation.id == operation_id.value)
                )
            ).one_or_none()
            if row is None:
                raise OperationLedgerError(f"Unknown operation {operation_id}")
            operation, quote = row
            wallet = (
                await session.get(Wallet, operation.wallet_id)
                if operation.wallet_id is not None
                else None
            )
            return OperationSnapshot(
                operation_id=operation_id,
                quote=self._domain_quote(quote),
                provider_model_id=quote.provider_model_id,
                request_key=quote.request_key,
                requester_id=quote.requester_id,
                chat_id=quote.chat_id,
                thread_id=quote.thread_id,
                pricing_input=self._frozen_mapping(quote.pricing_input),
                state=OperationState(operation.state),
                delivery_state=DeliveryState(operation.delivery_state),
                wallet_owner=self._wallet_owner(wallet) if wallet is not None else None,
                funding_authorization=(
                    FundingAuthorization(operation.funding_authorization)
                    if operation.funding_authorization is not None
                    else None
                ),
                result_metadata=self._frozen_mapping(operation.result_metadata),
                terminal_reason=operation.terminal_reason,
                reserved_at=operation.reserved_at,
                execution_started_at=operation.execution_started_at,
                captured_at=operation.captured_at,
                released_at=operation.released_at,
                reversed_at=operation.reversed_at,
                created_at=operation.created_at,
                updated_at=operation.updated_at,
            )

    async def reserve(
        self,
        operation_id: OperationId,
        *,
        allow_personal_once: bool = False,
    ) -> ReservationResult:
        """Reserve one complete wallet, trying shared funds before personal funds."""
        now = self._aware_now()
        with logfire.span("wallet.reserve", operation_id=str(operation_id)):
            async with self._transactions() as session:
                operation = await self._locked_operation(session, operation_id)
                quote = await session.get(OperationQuote, operation.quote_id)
                if quote is None:
                    raise OperationLedgerError("Operation quote disappeared")

                if operation.state in {
                    OperationState.RESERVED.value,
                    OperationState.EXECUTING.value,
                    OperationState.CAPTURED.value,
                }:
                    return await self._existing_reservation(session, operation)
                if operation.state != OperationState.QUOTED.value:
                    return ReservationRejected(
                        operation_id, ReservationRejection.OPERATION_TERMINAL
                    )
                if now >= quote.expires_at:
                    operation.state = OperationState.CANCELED.value
                    operation.terminal_reason = ReservationRejection.QUOTE_EXPIRED.value
                    return ReservationRejected(
                        operation_id, ReservationRejection.QUOTE_EXPIRED
                    )

                chat = await session.get(Chat, quote.chat_id)
                if chat is None:
                    raise OperationLedgerError("Operation chat disappeared")
                candidates = await self._funding_candidates(
                    session,
                    quote,
                    chat,
                    allow_personal_once=allow_personal_once,
                )
                debt_seen = False
                for owner, authorization in candidates:
                    reserved, in_debt = await self._try_reserve_owner(
                        session,
                        operation,
                        quote.amount_credits,
                        owner,
                        authorization,
                        now,
                    )
                    debt_seen = debt_seen or in_debt
                    if reserved is not None:
                        return reserved

                has_consent = await self._has_personal_consent(
                    session, quote.requester_id, quote.chat_id
                )
                if (
                    chat.type != "private"
                    and not allow_personal_once
                    and not has_consent
                ):
                    reason = ReservationRejection.PERSONAL_CONSENT_REQUIRED
                elif debt_seen:
                    reason = ReservationRejection.WALLET_IN_DEBT
                else:
                    reason = ReservationRejection.INSUFFICIENT_FUNDS
                return ReservationRejected(operation_id, reason)

    async def mark_executing(self, operation_id: OperationId) -> SettlementResult:
        """Atomically claim provider work, which only ``changed=True`` authorizes."""
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state == OperationState.EXECUTING.value:
                return SettlementResult(operation_id, OperationState.EXECUTING, False)
            if operation.state != OperationState.RESERVED.value:
                raise InvalidOperationTransitionError(
                    f"Cannot execute operation in {operation.state}"
                )
            operation.state = OperationState.EXECUTING.value
            operation.execution_started_at = self._aware_now()
            return SettlementResult(operation_id, OperationState.EXECUTING, True)

    async def cancel(
        self,
        operation_id: OperationId,
        *,
        reason: str,
    ) -> SettlementResult:
        """Cancel pre-execution work without releasing an in-flight provider claim."""
        if not reason.strip():
            raise ValueError("cancellation reason must not be blank")
        now = self._aware_now()
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state == OperationState.CANCELED.value:
                return SettlementResult(operation_id, OperationState.CANCELED, False)
            if operation.state == OperationState.RELEASED.value:
                return SettlementResult(operation_id, OperationState.RELEASED, False)
            if operation.state == OperationState.QUOTED.value:
                operation.state = OperationState.CANCELED.value
                operation.terminal_reason = reason
                return SettlementResult(operation_id, OperationState.CANCELED, True)
            if operation.state == OperationState.RESERVED.value:
                return await self._release_locked(session, operation, reason, now)
            if operation.state == OperationState.EXECUTING.value:
                raise InvalidOperationTransitionError(
                    "Cannot cancel an executing operation until provider termination "
                    "is confirmed; release it after a definitive failure"
                )
            raise InvalidOperationTransitionError(
                f"Cannot cancel operation in {operation.state}"
            )

    async def capture(self, operation_id: OperationId) -> SettlementResult:
        """Convert an existing reservation to consumed inventory once."""
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state == OperationState.CAPTURED.value:
                return SettlementResult(operation_id, OperationState.CAPTURED, False)
            if operation.state not in {
                OperationState.RESERVED.value,
                OperationState.EXECUTING.value,
            }:
                raise InvalidOperationTransitionError(
                    f"Cannot capture operation in {operation.state}"
                )
            return await self._capture_locked(
                session,
                operation,
                self._aware_now(),
            )

    async def expire_quote_if_due(
        self,
        operation_id: OperationId,
        *,
        as_of: datetime,
    ) -> SettlementResult | None:
        """Cancel a still-quoted operation only when its immutable quote is due."""
        self._require_aware(as_of, "as_of")
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state != OperationState.QUOTED.value:
                return None
            quote = await session.get(OperationQuote, operation.quote_id)
            if quote is None:
                raise OperationLedgerError("Operation quote disappeared")
            if quote.expires_at > as_of:
                return None
            operation.state = OperationState.CANCELED.value
            operation.terminal_reason = ReservationRejection.QUOTE_EXPIRED.value
            return SettlementResult(operation_id, OperationState.CANCELED, True)

    async def release_stale_reservation(
        self,
        operation_id: OperationId,
        *,
        stale_before: datetime,
        reason: str,
    ) -> SettlementResult | None:
        """Release only a reservation that is still unclaimed and stale."""
        self._require_aware(stale_before, "stale_before")
        if not reason.strip():
            raise ValueError("release reason must not be blank")
        now = self._aware_now()
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state != OperationState.RESERVED.value:
                return None
            if operation.reserved_at is None or operation.reserved_at > stale_before:
                return None
            return await self._release_locked(session, operation, reason, now)

    async def release_stale_execution_without_result(
        self,
        operation_id: OperationId,
        *,
        stale_before: datetime,
        reason: str,
    ) -> SettlementResult | None:
        """Release a stale provider claim only while no durable result exists.

        The operation row lock serializes this check with result persistence. If
        persistence wins, its artifact prevents release; if release wins, later
        persistence rejects the terminal operation and cleans its orphaned bytes.
        """
        self._require_aware(stale_before, "stale_before")
        if not reason.strip():
            raise ValueError("release reason must not be blank")
        now = self._aware_now()
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state != OperationState.EXECUTING.value:
                return None
            if (
                operation.execution_started_at is None
                or operation.execution_started_at > stale_before
            ):
                return None
            has_artifact = bool(
                await session.scalar(
                    select(exists().where(Artifact.operation_id == operation.id))
                )
            )
            if has_artifact:
                return None
            return await self._release_locked(session, operation, reason, now)

    async def capture_persisted_result(
        self,
        operation_id: OperationId,
    ) -> SettlementResult | None:
        """Capture only when an artifact and its delivery intent are committed."""
        now = self._aware_now()
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state not in {
                OperationState.EXECUTING.value,
                OperationState.CAPTURED.value,
            }:
                return None
            if operation.wallet_id is None:
                return None
            has_result = bool(
                await session.scalar(
                    select(
                        exists().where(
                            Artifact.operation_id == operation.id,
                            exists().where(DeliveryIntent.operation_id == operation.id),
                        )
                    )
                )
            )
            if not has_result:
                return None
            if operation.state == OperationState.CAPTURED.value:
                return SettlementResult(operation_id, OperationState.CAPTURED, False)
            return await self._capture_locked(session, operation, now)

    async def release(
        self,
        operation_id: OperationId,
        *,
        reason: str,
    ) -> SettlementResult:
        """Return an uncaptured reservation to its exact source inventories."""
        if not reason.strip():
            raise ValueError("release reason must not be blank")
        now = self._aware_now()
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state == OperationState.RELEASED.value:
                return SettlementResult(operation_id, OperationState.RELEASED, False)
            if operation.state not in {
                OperationState.RESERVED.value,
                OperationState.EXECUTING.value,
            }:
                raise InvalidOperationTransitionError(
                    f"Cannot release operation in {operation.state}"
                )
            return await self._release_locked(session, operation, reason, now)

    async def _release_locked(
        self,
        session: AsyncSession,
        operation: PaidOperation,
        reason: str,
        now: datetime,
    ) -> SettlementResult:
        """Release a locked reserved operation inside the caller's transaction."""
        operation_id = OperationId(operation.id)
        if operation.state not in {
            OperationState.RESERVED.value,
            OperationState.EXECUTING.value,
        }:
            raise InvalidOperationTransitionError(
                f"Cannot release operation in {operation.state}"
            )
        wallet, allocations = await self._locked_allocations(session, operation)
        for allocation, lot in allocations:
            if lot.reserved_credits < allocation.amount_credits:
                raise OperationLedgerError("Reserved lot balance is inconsistent")
            lot.reserved_credits -= allocation.amount_credits
            recovery = await self._return_to_source(
                session, wallet, lot, allocation.amount_credits, now
            )
            await self._record_debt_restorations(
                session,
                wallet,
                operation,
                recovery,
                reason=reason,
            )
            self._record_lot_event(
                session,
                wallet,
                lot,
                operation,
                "release",
                allocation.amount_credits,
                reason=reason,
            )
        operation.state = OperationState.RELEASED.value
        operation.released_at = now
        operation.terminal_reason = reason
        return SettlementResult(operation_id, OperationState.RELEASED, True)

    async def _capture_locked(
        self,
        session: AsyncSession,
        operation: PaidOperation,
        now: datetime,
    ) -> SettlementResult:
        """Capture a locked reserved operation inside the caller's transaction."""
        operation_id = OperationId(operation.id)
        if operation.state not in {
            OperationState.RESERVED.value,
            OperationState.EXECUTING.value,
        }:
            raise InvalidOperationTransitionError(
                f"Cannot capture operation in {operation.state}"
            )
        wallet, allocations = await self._locked_allocations(session, operation)
        for allocation, lot in allocations:
            if lot.reserved_credits < allocation.amount_credits:
                raise OperationLedgerError("Reserved lot balance is inconsistent")
            lot.reserved_credits -= allocation.amount_credits
            lot.consumed_credits += allocation.amount_credits
            self._record_lot_event(
                session,
                wallet,
                lot,
                operation,
                "capture",
                allocation.amount_credits,
            )
        operation.state = OperationState.CAPTURED.value
        operation.captured_at = now
        return SettlementResult(operation_id, OperationState.CAPTURED, True)

    async def reverse(
        self,
        operation_id: OperationId,
        *,
        reason: str,
    ) -> SettlementResult:
        """Refund captured spend to the same inventory without negative entries."""
        if not reason.strip():
            raise ValueError("reversal reason must not be blank")
        now = self._aware_now()
        async with self._transactions() as session:
            operation = await self._locked_operation(session, operation_id)
            if operation.state == OperationState.REVERSED.value:
                return SettlementResult(operation_id, OperationState.REVERSED, False)
            if operation.state != OperationState.CAPTURED.value:
                raise InvalidOperationTransitionError(
                    f"Cannot reverse operation in {operation.state}"
                )
            wallet, allocations = await self._locked_allocations(session, operation)
            for allocation, lot in allocations:
                if lot.consumed_credits < allocation.amount_credits:
                    raise OperationLedgerError("Consumed lot balance is inconsistent")
                lot.consumed_credits -= allocation.amount_credits
                recovery = await self._return_to_source(
                    session, wallet, lot, allocation.amount_credits, now
                )
                await self._record_debt_restorations(
                    session,
                    wallet,
                    operation,
                    recovery,
                    reason=reason,
                )
                self._record_lot_event(
                    session,
                    wallet,
                    lot,
                    operation,
                    "reversal",
                    allocation.amount_credits,
                    reason=reason,
                )
            operation.state = OperationState.REVERSED.value
            operation.reversed_at = now
            operation.terminal_reason = reason
            return SettlementResult(operation_id, OperationState.REVERSED, True)

    async def balance(self, owner: WalletOwner) -> WalletBalance:
        """Return inventory totals without mutating or silently expiring credits."""
        now = self._aware_now()
        async with self._transactions() as session:
            _, balance = await self._balance_in_session(session, owner, now)
            return balance

    async def statement(
        self,
        owner: WalletOwner,
        *,
        activity_limit: int = 3,
    ) -> WalletStatement:
        """Return one consistent, bounded balance statement for Telegram UI."""
        if (
            isinstance(activity_limit, bool)
            or not isinstance(activity_limit, int)
            or not 0 <= activity_limit <= 10
        ):
            raise ValueError("activity_limit must be between zero and ten")
        now = self._aware_now()
        async with self._transactions() as session:
            wallet, balance = await self._balance_in_session(session, owner, now)
            if wallet is None:
                return WalletStatement(balance)

            allowance_period_end = None
            renewal_enabled = None
            if owner.kind is WalletOwnerKind.USER:
                subscription = await session.scalar(
                    select(Subscription).where(Subscription.user_id == owner.id)
                )
                if (
                    subscription is not None
                    and subscription.status in {"active", "canceled"}
                    and subscription.current_period_end > now
                ):
                    allowance_period_end = subscription.current_period_end
                    renewal_enabled = bool(
                        subscription.status == "active" and subscription.renewal_enabled
                    )

            recent_activity = await self._recent_activity(
                session,
                wallet.id,
                limit=activity_limit,
            )
            return WalletStatement(
                balance=balance,
                allowance_period_end=allowance_period_end,
                renewal_enabled=renewal_enabled,
                recent_activity=recent_activity,
            )

    @staticmethod
    async def _balance_in_session(
        session: AsyncSession,
        owner: WalletOwner,
        now: datetime,
    ) -> tuple[Wallet | None, WalletBalance]:
        owner_column = (
            Wallet.user_id if owner.kind is WalletOwnerKind.USER else Wallet.chat_id
        )
        wallet = await session.scalar(select(Wallet).where(owner_column == owner.id))
        if wallet is None:
            return None, WalletBalance(owner, 0, 0, 0, 0, 0)
        rows = (
            await session.execute(
                select(
                    WalletLot.kind,
                    func.sum(
                        case(
                            (
                                (WalletLot.expires_at.is_(None))
                                | (WalletLot.expires_at > now),
                                WalletLot.available_credits,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(WalletLot.reserved_credits),
                    func.sum(WalletLot.consumed_credits),
                )
                .where(WalletLot.wallet_id == wallet.id)
                .group_by(WalletLot.kind)
            )
        ).all()
        by_kind = {
            kind: (int(available), int(reserved), int(consumed))
            for kind, available, reserved, consumed in rows
        }
        allowance = by_kind.get(InventoryKind.ALLOWANCE.value, (0, 0, 0))
        purchased = by_kind.get(InventoryKind.PURCHASED.value, (0, 0, 0))
        return wallet, WalletBalance(
            owner=owner,
            allowance_available=allowance[0],
            purchased_available=purchased[0],
            reserved=allowance[1] + purchased[1],
            consumed=allowance[2] + purchased[2],
            debt=wallet.debt_credits,
        )

    @staticmethod
    async def _recent_activity(
        session: AsyncSession,
        wallet_id: uuid.UUID,
        *,
        limit: int,
    ) -> tuple[WalletActivity, ...]:
        if limit == 0:
            return ()
        activity_id = func.coalesce(
            WalletLedgerEntry.operation_id,
            WalletLedgerEntry.payment_receipt_id,
            WalletLedgerEntry.id,
        )
        occurred_at = func.max(WalletLedgerEntry.created_at)
        rows = (
            await session.execute(
                select(
                    WalletLedgerEntry.event_type,
                    activity_id.label("activity_id"),
                    func.sum(WalletLedgerEntry.amount_credits).label("credits"),
                    occurred_at.label("occurred_at"),
                    func.max(OperationQuote.feature).label("feature"),
                )
                .outerjoin(
                    PaidOperation,
                    PaidOperation.id == WalletLedgerEntry.operation_id,
                )
                .outerjoin(
                    OperationQuote,
                    OperationQuote.id == PaidOperation.quote_id,
                )
                .where(
                    WalletLedgerEntry.wallet_id == wallet_id,
                    WalletLedgerEntry.event_type.in_(_ACTIVITY_KIND_BY_EVENT),
                )
                .group_by(WalletLedgerEntry.event_type, activity_id)
                .order_by(occurred_at.desc(), activity_id.desc())
                .limit(limit)
            )
        ).all()
        return tuple(
            WalletActivity(
                kind=_ACTIVITY_KIND_BY_EVENT[row.event_type],
                credits=int(row.credits),
                occurred_at=row.occurred_at,
                feature=Feature(row.feature) if row.feature is not None else None,
            )
            for row in rows
        )

    async def personal_consent_enabled(
        self, user_id: uuid.UUID, chat_id: uuid.UUID
    ) -> bool:
        """Return whether durable personal fallback is enabled in one chat."""
        async with self._transactions() as session:
            return await self._has_personal_consent(session, user_id, chat_id)

    async def grant_personal_consent(
        self, user_id: uuid.UUID, chat_id: uuid.UUID
    ) -> None:
        """Enable durable personal fallback for exactly one user and chat."""
        now = self._aware_now()
        async with self._transactions() as session:
            await session.execute(
                insert(PersonalSpendConsent)
                .values(
                    user_id=user_id,
                    chat_id=chat_id,
                    enabled=True,
                    granted_at=now,
                    revoked_at=None,
                )
                .on_conflict_do_update(
                    index_elements=[
                        PersonalSpendConsent.user_id,
                        PersonalSpendConsent.chat_id,
                    ],
                    set_={
                        "enabled": True,
                        "granted_at": now,
                        "revoked_at": None,
                        "updated_at": now,
                    },
                )
            )

    async def revoke_personal_consent(
        self, user_id: uuid.UUID, chat_id: uuid.UUID
    ) -> None:
        """Revoke durable personal fallback without deleting its audit state."""
        now = self._aware_now()
        async with self._transactions() as session:
            consent = await session.get(
                PersonalSpendConsent, (user_id, chat_id), with_for_update=True
            )
            if consent is None:
                return
            consent.enabled = False
            consent.revoked_at = now

    async def _funding_candidates(
        self,
        session: AsyncSession,
        quote: OperationQuote,
        chat: Chat,
        *,
        allow_personal_once: bool,
    ) -> list[tuple[WalletOwner, FundingAuthorization]]:
        user_owner = WalletOwner(WalletOwnerKind.USER, quote.requester_id)
        if chat.type == "private":
            return [(user_owner, FundingAuthorization.PRIVATE)]

        candidates: list[tuple[WalletOwner, FundingAuthorization]] = []
        if chat.shared_credit_spending_enabled:
            candidates.append(
                (
                    WalletOwner(WalletOwnerKind.CHAT, quote.chat_id),
                    FundingAuthorization.CHAT,
                )
            )
        if allow_personal_once:
            candidates.append((user_owner, FundingAuthorization.ONCE))
        elif await self._has_personal_consent(
            session, quote.requester_id, quote.chat_id
        ):
            candidates.append((user_owner, FundingAuthorization.ALWAYS))
        return candidates

    async def _try_reserve_owner(
        self,
        session: AsyncSession,
        operation: PaidOperation,
        amount: int,
        owner: WalletOwner,
        authorization: FundingAuthorization,
        now: datetime,
    ) -> tuple[ReservedOperation | None, bool]:
        wallet = await self._locked_wallet(session, owner)
        if wallet.debt_credits > 0:
            return None, True

        lots = list(
            await session.scalars(
                select(WalletLot)
                .where(
                    WalletLot.wallet_id == wallet.id,
                    WalletLot.available_credits > 0,
                )
                .order_by(
                    case((WalletLot.kind == InventoryKind.ALLOWANCE.value, 0), else_=1),
                    WalletLot.expires_at.asc().nulls_last(),
                    WalletLot.created_at,
                    WalletLot.id,
                )
                .with_for_update()
            )
        )
        eligible: list[WalletLot] = []
        for lot in lots:
            if lot.expires_at is not None and lot.expires_at <= now:
                expired = lot.available_credits
                lot.available_credits = 0
                lot.expired_credits += expired
                self._record_lot_event(session, wallet, lot, None, "expire", expired)
            else:
                eligible.append(lot)

        if sum(lot.available_credits for lot in eligible) < amount:
            return None, False

        operation.wallet_id = wallet.id
        operation.funding_authorization = authorization.value
        operation.state = OperationState.RESERVED.value
        operation.reserved_at = now
        await session.flush([operation])

        remaining = amount
        allowance_credits = 0
        purchased_credits = 0
        for lot in eligible:
            if remaining == 0:
                break
            allocated = min(lot.available_credits, remaining)
            lot.available_credits -= allocated
            lot.reserved_credits += allocated
            session.add(
                OperationAllocation(
                    operation_id=operation.id,
                    wallet_id=wallet.id,
                    wallet_lot_id=lot.id,
                    amount_credits=allocated,
                )
            )
            self._record_lot_event(
                session, wallet, lot, operation, "reserve", allocated
            )
            if lot.kind == InventoryKind.ALLOWANCE.value:
                allowance_credits += allocated
            else:
                purchased_credits += allocated
            remaining -= allocated

        allocation = InventoryAllocation(
            owner=owner,
            allowance_credits=allowance_credits,
            purchased_credits=purchased_credits,
        )
        return (
            ReservedOperation(
                operation_id=OperationId(operation.id),
                allocation=allocation,
                authorization=authorization,
            ),
            False,
        )

    async def _locked_wallet(self, session: AsyncSession, owner: WalletOwner) -> Wallet:
        values = (
            {"user_id": owner.id}
            if owner.kind is WalletOwnerKind.USER
            else {"chat_id": owner.id}
        )
        await session.execute(insert(Wallet).values(**values).on_conflict_do_nothing())
        owner_column = (
            Wallet.user_id if owner.kind is WalletOwnerKind.USER else Wallet.chat_id
        )
        wallet = await session.scalar(
            select(Wallet).where(owner_column == owner.id).with_for_update()
        )
        if wallet is None:
            raise OperationLedgerError("Wallet creation failed")
        return wallet

    async def _locked_operation(
        self, session: AsyncSession, operation_id: OperationId
    ) -> PaidOperation:
        operation = await session.scalar(
            select(PaidOperation)
            .where(PaidOperation.id == operation_id.value)
            .with_for_update()
        )
        if operation is None:
            raise OperationLedgerError(f"Unknown operation {operation_id}")
        return operation

    async def _locked_allocations(
        self, session: AsyncSession, operation: PaidOperation
    ) -> tuple[Wallet, list[tuple[OperationAllocation, WalletLot]]]:
        if operation.wallet_id is None:
            raise OperationLedgerError("Settled operation has no wallet")
        wallet = await session.scalar(
            select(Wallet).where(Wallet.id == operation.wallet_id).with_for_update()
        )
        if wallet is None:
            raise OperationLedgerError("Operation wallet disappeared")
        allocations = list(
            (
                await session.execute(
                    select(OperationAllocation, WalletLot)
                    .join(WalletLot, WalletLot.id == OperationAllocation.wallet_lot_id)
                    .where(OperationAllocation.operation_id == operation.id)
                    .order_by(WalletLot.created_at, WalletLot.id)
                    .with_for_update(of=WalletLot)
                )
            ).tuples()
        )
        if not allocations:
            raise OperationLedgerError("Settled operation has no allocations")
        return wallet, allocations

    async def _existing_reservation(
        self, session: AsyncSession, operation: PaidOperation
    ) -> ReservedOperation:
        wallet, allocations = await self._locked_allocations(session, operation)
        allowance = sum(
            allocation.amount_credits
            for allocation, lot in allocations
            if lot.kind == InventoryKind.ALLOWANCE.value
        )
        purchased = sum(
            allocation.amount_credits
            for allocation, lot in allocations
            if lot.kind == InventoryKind.PURCHASED.value
        )
        if operation.funding_authorization is None:
            raise OperationLedgerError("Reserved operation has no authorization")
        return ReservedOperation(
            operation_id=OperationId(operation.id),
            allocation=InventoryAllocation(
                self._wallet_owner(wallet), allowance, purchased
            ),
            authorization=FundingAuthorization(operation.funding_authorization),
            idempotent=True,
        )

    async def _return_to_source(
        self,
        session: AsyncSession,
        wallet: Wallet,
        lot: WalletLot,
        amount: int,
        now: datetime,
    ) -> DebtRecovery | None:
        with session.no_autoflush:
            source_was_clawed_back = await self._source_was_clawed_back(session, lot)
        if source_was_clawed_back:
            lot.clawed_back_credits += amount
            return await WalletDebtProvenance.recover(
                session,
                wallet,
                lot,
                amount,
                now,
            )
        elif lot.expires_at is not None and lot.expires_at <= now:
            lot.expired_credits += amount
        else:
            lot.available_credits += amount
        return None

    @staticmethod
    async def _record_debt_restorations(
        session: AsyncSession,
        wallet: Wallet,
        operation: PaidOperation,
        recovery: DebtRecovery | None,
        *,
        reason: str,
    ) -> None:
        if recovery is None:
            return
        for restoration in recovery.restorations:
            lot = await session.get(WalletLot, restoration.repayment_wallet_lot_id)
            if lot is None:
                raise OperationLedgerError("Debt repayment lot disappeared")
            OperationLedger._record_lot_event(
                session,
                wallet,
                lot,
                operation,
                "debt_restored",
                restoration.credits,
                reason=reason,
                key_suffix=str(restoration.debt_source_id),
                metadata={
                    "debt_source_id": str(restoration.debt_source_id),
                    "source_wallet_lot_id": str(restoration.source_wallet_lot_id),
                    "repayment_wallet_lot_id": str(restoration.repayment_wallet_lot_id),
                },
            )

    @staticmethod
    async def _source_was_clawed_back(session: AsyncSession, lot: WalletLot) -> bool:
        if lot.payment_receipt_id is not None:
            status = await session.scalar(
                select(PaymentReceipt.status).where(
                    PaymentReceipt.id == lot.payment_receipt_id
                )
            )
            return status == "clawed_back"
        if lot.subscription_cycle_id is not None:
            status = await session.scalar(
                select(SubscriptionCycle.status).where(
                    SubscriptionCycle.id == lot.subscription_cycle_id
                )
            )
            return status == "clawed_back"
        return False

    @staticmethod
    async def _has_personal_consent(
        session: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID
    ) -> bool:
        return bool(
            await session.scalar(
                select(PersonalSpendConsent.enabled).where(
                    PersonalSpendConsent.user_id == user_id,
                    PersonalSpendConsent.chat_id == chat_id,
                    PersonalSpendConsent.enabled.is_(True),
                )
            )
        )

    @staticmethod
    def _wallet_owner(wallet: Wallet) -> WalletOwner:
        if wallet.user_id is not None:
            return WalletOwner(WalletOwnerKind.USER, wallet.user_id)
        if wallet.chat_id is not None:
            return WalletOwner(WalletOwnerKind.CHAT, wallet.chat_id)
        raise OperationLedgerError("Wallet has no owner")

    @staticmethod
    def _record_lot_event(
        session: AsyncSession,
        wallet: Wallet,
        lot: WalletLot,
        operation: PaidOperation | None,
        event_type: str,
        amount: int,
        *,
        reason: str | None = None,
        key_suffix: str = "",
        metadata: dict[str, object] | None = None,
    ) -> None:
        operation_key = str(operation.id) if operation is not None else "none"
        suffix = f":{key_suffix}" if key_suffix else ""
        session.add(
            WalletLedgerEntry(
                wallet_id=wallet.id,
                wallet_lot_id=lot.id,
                operation_id=operation and operation.id,
                event_type=event_type,
                amount_credits=amount,
                available_after=lot.available_credits,
                reserved_after=lot.reserved_credits,
                consumed_after=lot.consumed_credits,
                wallet_debt_after=wallet.debt_credits,
                idempotency_key=f"{event_type}:{operation_key}:{lot.id}{suffix}",
                reason=reason,
                metadata_=metadata or {},
            )
        )

    @staticmethod
    def _quote_matches(stored: OperationQuote, values: Mapping[str, object]) -> bool:
        immutable_fields = (
            "operation_id",
            "request_key",
            "requester_id",
            "chat_id",
            "thread_id",
            "feature",
            "model_key",
            "provider",
            "provider_model_id",
            "context_band",
            "variant",
            "amount_credits",
            "estimated_provider_cost_usd",
            "pricing_version",
            "catalog_verified_on",
            "pricing_input",
        )
        return all(
            getattr(stored, field) == values[field] for field in immutable_fields
        )

    @staticmethod
    async def _quote_for_retry(
        session: AsyncSession,
        *,
        operation_id: OperationId,
        request_key: str,
    ) -> OperationQuote | None:
        by_operation = await session.scalar(
            select(OperationQuote).where(
                OperationQuote.operation_id == operation_id.value
            )
        )
        by_request = await session.scalar(
            select(OperationQuote).where(OperationQuote.request_key == request_key)
        )
        if (
            by_operation is not None
            and by_request is not None
            and by_operation.id != by_request.id
        ):
            raise ImmutableQuoteConflictError(
                f"Operation {operation_id} and request key identify different quotes"
            )
        return by_operation or by_request

    @staticmethod
    def _domain_quote(stored: OperationQuote) -> Quote:
        return Quote(
            id=QuoteId(stored.id),
            operation_id=OperationId(stored.operation_id),
            key=QuoteKey(
                feature=Feature(stored.feature),
                model_key=ModelRole(stored.model_key),
                context_band=ContextBand(stored.context_band),
                variant=stored.variant,
            ),
            provider=InferenceProvider(stored.provider),
            provider_model_id=stored.provider_model_id,
            credits=stored.amount_credits,
            estimated_provider_cost_usd=stored.estimated_provider_cost_usd,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            pricing_version=stored.pricing_version,
            catalog_verified_on=stored.catalog_verified_on,
        )

    @staticmethod
    def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
        return MappingProxyType(deepcopy(dict(value)))

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("OperationLedger clock must return an aware datetime")
        return now
