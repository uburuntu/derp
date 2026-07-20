"""Atomic wallet selection, reservation, and settlement."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol

import logfire
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.catalog import get_google_model
from derp.models import (
    Chat,
    OperationAllocation,
    OperationQuote,
    PaidOperation,
    PaymentReceipt,
    PersonalSpendConsent,
    SubscriptionCycle,
    Wallet,
    WalletLedgerEntry,
    WalletLot,
)
from derp.operations.types import (
    FundingAuthorization,
    InventoryAllocation,
    InventoryKind,
    OperationId,
    OperationState,
    Quote,
    ReservationRejected,
    ReservationRejection,
    ReservationResult,
    ReservedOperation,
    SettlementResult,
    WalletBalance,
    WalletOwner,
    WalletOwnerKind,
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
        """Persist immutable quote terms and the corresponding operation once."""
        if not request_key.strip():
            raise ValueError("request_key must not be blank")
        if thread_id is not None and thread_id <= 0:
            raise ValueError("thread_id must be positive")

        model = get_google_model(quote.key.model_key)
        quote_values = {
            "id": quote.id.value,
            "operation_id": quote.operation_id.value,
            "request_key": request_key,
            "requester_id": requester_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "feature": quote.key.feature.value,
            "model_key": quote.key.model_key.value,
            "provider_model_id": model.provider_model_id,
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
                stored_quote = await session.scalar(
                    select(OperationQuote).where(
                        OperationQuote.operation_id == quote.operation_id.value
                    )
                )
                if stored_quote is None or not self._quote_matches(
                    stored_quote, quote_values
                ):
                    raise ImmutableQuoteConflictError(
                        f"Conflicting quote for operation {quote.operation_id}"
                    )
                quote_id = stored_quote.id
            else:
                quote_id = created_quote_id

            await session.execute(
                insert(PaidOperation)
                .values(
                    id=quote.operation_id.value,
                    quote_id=quote_id,
                    state=OperationState.QUOTED.value,
                )
                .on_conflict_do_nothing(index_elements=[PaidOperation.id])
            )
        return quote.operation_id

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
        """Close the reservation transaction before provider execution begins."""
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
            operation.captured_at = self._aware_now()
            return SettlementResult(operation_id, OperationState.CAPTURED, True)

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
            wallet, allocations = await self._locked_allocations(session, operation)
            for allocation, lot in allocations:
                if lot.reserved_credits < allocation.amount_credits:
                    raise OperationLedgerError("Reserved lot balance is inconsistent")
                lot.reserved_credits -= allocation.amount_credits
                await self._return_to_source(
                    session, wallet, lot, allocation.amount_credits, now
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
                await self._return_to_source(
                    session, wallet, lot, allocation.amount_credits, now
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
        async with self._transactions() as session:
            owner_column = (
                Wallet.user_id if owner.kind is WalletOwnerKind.USER else Wallet.chat_id
            )
            wallet = await session.scalar(
                select(Wallet).where(owner_column == owner.id)
            )
            if wallet is None:
                return WalletBalance(owner, 0, 0, 0, 0, 0)
            rows = (
                await session.execute(
                    select(
                        WalletLot.kind,
                        func.sum(WalletLot.available_credits),
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
            return WalletBalance(
                owner=owner,
                allowance_available=allowance[0],
                purchased_available=purchased[0],
                reserved=allowance[1] + purchased[1],
                consumed=allowance[2] + purchased[2],
                debt=wallet.debt_credits,
            )

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
    ) -> None:
        with session.no_autoflush:
            source_was_clawed_back = await self._source_was_clawed_back(session, lot)
        if source_was_clawed_back:
            lot.clawed_back_credits += amount
            wallet.debt_credits = max(0, wallet.debt_credits - amount)
        elif lot.expires_at is not None and lot.expires_at <= now:
            lot.expired_credits += amount
        else:
            lot.available_credits += amount

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
    ) -> None:
        operation_key = str(operation.id) if operation is not None else "none"
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
                idempotency_key=f"{event_type}:{operation_key}:{lot.id}",
                reason=reason,
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
            "provider_model_id",
            "context_band",
            "variant",
            "amount_credits",
            "estimated_provider_cost_usd",
            "pricing_version",
            "catalog_verified_on",
            "pricing_input",
            "expires_at",
            "created_at",
        )
        return all(
            getattr(stored, field) == values[field] for field in immutable_fields
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("OperationLedger clock must return an aware datetime")
        return now
