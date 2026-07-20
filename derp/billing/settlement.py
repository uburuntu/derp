"""Idempotent payment fulfillment, subscription cycles, and clawbacks."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from derp.billing.payloads import hash_invoice_payload
from derp.billing.products import (
    DEFAULT_PRODUCT_CATALOG,
    ProductCatalog,
    StarsProduct,
    SubscriptionPlan,
)
from derp.billing.types import (
    CapturedPayment,
    ClawbackResult,
    FulfillmentResult,
    FulfillmentState,
    PaymentConflictError,
    ProductKind,
    SubscriptionStateError,
    SubscriptionStateResult,
    UnknownProductError,
)
from derp.models import (
    Chat,
    PaymentReceipt,
    PurchaseIntent,
    Subscription,
    SubscriptionCycle,
    User,
    Wallet,
    WalletLedgerEntry,
    WalletLot,
)

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _ReviewRequiredError(RuntimeError):
    pass


class PaymentSettlementService:
    """Own short transactions from captured Stars to exact wallet inventory."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        catalog: ProductCatalog = DEFAULT_PRODUCT_CATALOG,
        clock: Clock = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._catalog = catalog
        self._clock = clock

    async def fulfill(self, payment: CapturedPayment) -> FulfillmentResult:
        """Persist a charge once and fulfill only fully matching intent terms."""
        if payment.currency != "XTR":
            raise ValueError("Stars fulfillment requires XTR")
        now = self._aware_now()
        payload_hash = hash_invoice_payload(payment.invoice_payload)
        async with self._transactions() as session:
            existing = await session.scalar(
                select(PaymentReceipt)
                .where(PaymentReceipt.telegram_charge_id == payment.telegram_charge_id)
                .with_for_update()
            )
            if existing is not None:
                self._assert_receipt_matches(existing, payment, payload_hash)
                return await self._result_for_receipt(
                    session,
                    existing,
                    idempotent=True,
                )

            intent_reference = (
                await session.execute(
                    select(PurchaseIntent.id, PurchaseIntent.payer_user_id).where(
                        PurchaseIntent.token_hash == payload_hash
                    )
                )
            ).one_or_none()
            intent = None
            if intent_reference is not None:
                await session.scalar(
                    select(User.id)
                    .where(User.id == intent_reference.payer_user_id)
                    .with_for_update()
                )
                intent = await session.scalar(
                    select(PurchaseIntent)
                    .where(PurchaseIntent.id == intent_reference.id)
                    .with_for_update()
                )
            receipt_id = await session.scalar(
                insert(PaymentReceipt)
                .values(
                    purchase_intent_id=intent and intent.id,
                    telegram_charge_id=payment.telegram_charge_id,
                    provider_charge_id=payment.provider_charge_id,
                    payer_telegram_id=payment.payer_telegram_id,
                    currency=payment.currency,
                    total_amount=payment.total_amount,
                    payload_token_hash=payload_hash,
                    is_recurring=payment.is_recurring,
                    is_first_recurring=payment.is_first_recurring,
                    subscription_expiration_at=(payment.subscription_expiration_at),
                    status="received",
                )
                .on_conflict_do_nothing(
                    index_elements=[PaymentReceipt.telegram_charge_id]
                )
                .returning(PaymentReceipt.id)
            )
            if receipt_id is None:
                concurrent = await session.scalar(
                    select(PaymentReceipt)
                    .where(
                        PaymentReceipt.telegram_charge_id == payment.telegram_charge_id
                    )
                    .with_for_update()
                )
                if concurrent is None:
                    raise PaymentConflictError("Payment receipt conflict disappeared")
                self._assert_receipt_matches(concurrent, payment, payload_hash)
                return await self._result_for_receipt(
                    session,
                    concurrent,
                    idempotent=True,
                )
            receipt = await session.get(PaymentReceipt, receipt_id)
            if receipt is None:  # pragma: no cover - INSERT RETURNING contract
                raise RuntimeError("Payment receipt was not returned")

            if intent is None:
                receipt.status = "needs_review"
                return FulfillmentResult(
                    receipt.id,
                    FulfillmentState.NEEDS_REVIEW,
                    review_reason="unknown_intent",
                )

            review_reason = await self._review_reason(
                session,
                intent,
                payment,
                now,
            )
            if review_reason is not None:
                receipt.status = "needs_review"
                return FulfillmentResult(
                    receipt.id,
                    FulfillmentState.NEEDS_REVIEW,
                    intent_id=intent.id,
                    review_reason=review_reason,
                )

            product = self._resolve_product(intent)
            try:
                if product.kind is ProductKind.TOP_UP:
                    result = await self._fulfill_top_up(
                        session,
                        intent,
                        receipt,
                        product,
                        now,
                    )
                else:
                    result = await self._fulfill_subscription(
                        session,
                        intent,
                        receipt,
                        product,
                        payment,
                        now,
                    )
            except _ReviewRequiredError as exc:
                receipt.status = "needs_review"
                return FulfillmentResult(
                    receipt.id,
                    FulfillmentState.NEEDS_REVIEW,
                    intent_id=intent.id,
                    review_reason=str(exc),
                )
            return result

    async def set_subscription_renewal(
        self,
        user_id: uuid.UUID,
        *,
        enabled: bool,
    ) -> SubscriptionStateResult:
        """Record Telegram-confirmed cancellation or re-enablement."""
        now = self._aware_now()
        async with self._transactions() as session:
            subscription = await session.scalar(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .with_for_update()
            )
            if subscription is None:
                raise SubscriptionStateError("User has no subscription")
            if subscription.current_period_end <= now:
                raise SubscriptionStateError("Subscription cycle has expired")

            changed = subscription.renewal_enabled is not enabled
            if enabled:
                subscription.status = "active"
                subscription.renewal_enabled = True
                subscription.canceled_at = None
            else:
                subscription.status = "canceled"
                subscription.renewal_enabled = False
                subscription.canceled_at = subscription.canceled_at or now
            return SubscriptionStateResult(
                subscription.id,
                subscription.renewal_enabled,
                subscription.current_period_end,
                changed,
            )

    async def expire_due_cycles(self, *, limit: int = 100) -> int:
        """Expire non-rolling allowance while leaving reservations settleable."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        now = self._aware_now()
        async with self._transactions() as session:
            candidates = list(
                (
                    await session.execute(
                        select(
                            SubscriptionCycle.id,
                            SubscriptionCycle.subscription_id,
                        )
                        .where(
                            SubscriptionCycle.status == "active",
                            SubscriptionCycle.period_end <= now,
                        )
                        .order_by(
                            SubscriptionCycle.period_end,
                            SubscriptionCycle.id,
                        )
                        .limit(limit)
                    )
                ).tuples()
            )
            if not candidates:
                return 0
            subscription_ids = {candidate.subscription_id for candidate in candidates}
            subscriptions = list(
                await session.scalars(
                    select(Subscription)
                    .where(Subscription.id.in_(subscription_ids))
                    .order_by(Subscription.id)
                    .with_for_update()
                )
            )
            candidate_ids = [candidate.id for candidate in candidates]
            cycles = list(
                await session.scalars(
                    select(SubscriptionCycle)
                    .where(
                        SubscriptionCycle.id.in_(candidate_ids),
                        SubscriptionCycle.status == "active",
                        SubscriptionCycle.period_end <= now,
                    )
                    .order_by(SubscriptionCycle.period_end, SubscriptionCycle.id)
                    .with_for_update()
                )
            )
            for cycle in cycles:
                await self._expire_cycle(session, cycle)
            for subscription in subscriptions:
                if subscription.current_period_end <= now:
                    subscription.status = "expired"
            return len(cycles)

    async def clawback(
        self,
        telegram_charge_id: str,
    ) -> ClawbackResult:
        """Revoke only the charged source and turn committed use into debt."""
        if not telegram_charge_id.strip():
            raise ValueError("telegram_charge_id must not be blank")
        now = self._aware_now()
        async with self._transactions() as session:
            receipt = await session.scalar(
                select(PaymentReceipt)
                .where(PaymentReceipt.telegram_charge_id == telegram_charge_id)
                .with_for_update()
            )
            if receipt is None:
                raise LookupError("Payment receipt does not exist")
            if receipt.status == "clawed_back":
                existing = await self._clawback_result(session, receipt)
                return ClawbackResult(
                    receipt.id,
                    existing.wallet_id,
                    0,
                    0,
                    True,
                )

            cycle = await session.scalar(
                select(SubscriptionCycle)
                .where(SubscriptionCycle.payment_receipt_id == receipt.id)
                .with_for_update()
            )
            source = await self._locked_source_lot(
                session,
                WalletLot.payment_receipt_id == receipt.id,
            )
            if source is None and cycle is not None:
                source = await self._locked_source_lot(
                    session,
                    WalletLot.subscription_cycle_id == cycle.id,
                )
            receipt.status = "clawed_back"
            receipt.clawed_back_at = now
            if cycle is not None:
                cycle.status = "clawed_back"
            if source is None:
                return ClawbackResult(receipt.id, None, 0, 0, False)

            wallet, lot = source
            removed = lot.available_credits
            debt = lot.reserved_credits + lot.consumed_credits + lot.debt_offset_credits
            lot.available_credits = 0
            lot.clawed_back_credits += removed
            wallet.debt_credits += debt
            if removed:
                self._record_event(
                    session,
                    wallet,
                    lot,
                    receipt,
                    "clawback",
                    removed,
                )
            if debt:
                self._record_event(
                    session,
                    wallet,
                    lot,
                    receipt,
                    "debt_incurred",
                    debt,
                )
            return ClawbackResult(receipt.id, wallet.id, removed, debt, False)

    async def _review_reason(
        self,
        session: AsyncSession,
        intent: PurchaseIntent,
        payment: CapturedPayment,
        now: datetime,
    ) -> str | None:
        payer_id = await session.scalar(
            select(User.id).where(
                User.id == intent.payer_user_id,
                User.telegram_id == payment.payer_telegram_id,
            )
        )
        if payer_id is None:
            return "payer_mismatch"
        if payment.currency != intent.currency:
            return "currency_mismatch"
        if payment.total_amount != intent.stars:
            return "amount_mismatch"
        try:
            product = self._resolve_product(intent)
        except ValueError, UnknownProductError:
            return "product_mismatch"
        if not self._product_matches(intent, product):
            return "product_mismatch"
        if intent.target_user_id is not None:
            if (
                intent.target_user_id != intent.payer_user_id
                or await session.get(User, intent.target_user_id) is None
            ):
                return "target_mismatch"
        elif (
            intent.target_chat_id is None
            or product.kind is ProductKind.SUBSCRIPTION
            or await session.get(Chat, intent.target_chat_id) is None
        ):
            return "target_mismatch"

        if product.kind is ProductKind.TOP_UP:
            if payment.is_recurring or payment.subscription_expiration_at is not None:
                return "unexpected_subscription_fields"
            if intent.status != "prechecked":
                return "invalid_intent_status"
            return None

        if (
            not payment.is_recurring
            or payment.subscription_expiration_at is None
            or payment.subscription_expiration_at <= now
        ):
            return "invalid_subscription_fields"
        if payment.is_first_recurring:
            return None if intent.status == "prechecked" else "invalid_intent_status"
        return None if intent.status == "fulfilled" else "invalid_intent_status"

    async def _fulfill_top_up(
        self,
        session: AsyncSession,
        intent: PurchaseIntent,
        receipt: PaymentReceipt,
        product: StarsProduct,
        now: datetime,
    ) -> FulfillmentResult:
        if intent.status == "fulfilled":
            raise _ReviewRequiredError("intent_already_fulfilled")
        wallet, lot = await self._grant_lot(
            session,
            intent,
            receipt,
            credit_count=product.credits,
            kind="purchased",
        )
        receipt.status = "fulfilled"
        intent.status = "fulfilled"
        intent.fulfilled_at = now
        return FulfillmentResult(
            receipt.id,
            FulfillmentState.FULFILLED,
            intent_id=intent.id,
            wallet_id=wallet.id,
            wallet_lot_id=lot.id,
            available_credits=lot.available_credits,
            debt_offset_credits=lot.debt_offset_credits,
        )

    async def _fulfill_subscription(
        self,
        session: AsyncSession,
        intent: PurchaseIntent,
        receipt: PaymentReceipt,
        product: StarsProduct,
        payment: CapturedPayment,
        now: datetime,
    ) -> FulfillmentResult:
        if not isinstance(product, SubscriptionPlan):
            raise _ReviewRequiredError("subscription_product_mismatch")
        period_end = payment.subscription_expiration_at
        if period_end is None:  # guarded before dispatch
            raise _ReviewRequiredError("missing_subscription_expiration")
        period_start = period_end - timedelta(seconds=product.period_seconds)

        await session.scalar(
            select(User.id).where(User.id == intent.payer_user_id).with_for_update()
        )
        subscription = await session.scalar(
            select(Subscription)
            .where(Subscription.user_id == intent.payer_user_id)
            .with_for_update()
        )
        if subscription is not None:
            existing_cycle = await session.scalar(
                select(SubscriptionCycle).where(
                    SubscriptionCycle.subscription_id == subscription.id,
                    SubscriptionCycle.period_end == period_end,
                )
            )
            if existing_cycle is not None:
                raise _ReviewRequiredError("duplicate_subscription_cycle")
        if subscription is not None:
            if subscription.current_period_end > now and (
                subscription.plan_id != product.id
                or subscription.plan_version != product.version
            ):
                raise _ReviewRequiredError("active_plan_mismatch")
            if subscription.current_period_end > period_start:
                raise _ReviewRequiredError("overlapping_subscription_cycle")
        else:
            subscription = Subscription(
                user_id=intent.payer_user_id,
                plan_id=product.id,
                plan_version=product.version,
                status="active",
                renewal_enabled=True,
                current_period_end=period_end,
            )
            session.add(subscription)
            await session.flush()

        prior_cycles = list(
            await session.scalars(
                select(SubscriptionCycle)
                .where(
                    SubscriptionCycle.subscription_id == subscription.id,
                    SubscriptionCycle.status == "active",
                )
                .with_for_update()
            )
        )
        for prior in prior_cycles:
            if prior.period_end > period_start:
                raise _ReviewRequiredError("overlapping_subscription_cycle")
            await self._expire_cycle(session, prior)

        cycle = SubscriptionCycle(
            subscription_id=subscription.id,
            payment_receipt_id=receipt.id,
            period_start=period_start,
            period_end=period_end,
            allowance_credits=product.allowance_credits,
            status="active",
            created_at=now,
        )
        session.add(cycle)
        await session.flush()
        wallet, lot = await self._grant_lot(
            session,
            intent,
            receipt,
            credit_count=product.allowance_credits,
            kind="allowance",
            cycle=cycle,
        )
        subscription.plan_id = product.id
        subscription.plan_version = product.version
        subscription.status = "active"
        subscription.renewal_enabled = True
        subscription.current_period_end = period_end
        subscription.canceled_at = None
        receipt.status = "fulfilled"
        if intent.status != "fulfilled":
            intent.status = "fulfilled"
            intent.fulfilled_at = now
        return FulfillmentResult(
            receipt.id,
            FulfillmentState.FULFILLED,
            intent_id=intent.id,
            wallet_id=wallet.id,
            wallet_lot_id=lot.id,
            subscription_id=subscription.id,
            subscription_cycle_id=cycle.id,
            available_credits=lot.available_credits,
            debt_offset_credits=lot.debt_offset_credits,
        )

    async def _grant_lot(
        self,
        session: AsyncSession,
        intent: PurchaseIntent,
        receipt: PaymentReceipt,
        *,
        credit_count: int,
        kind: str,
        cycle: SubscriptionCycle | None = None,
    ) -> tuple[Wallet, WalletLot]:
        wallet = await self._locked_target_wallet(session, intent)
        debt_offset = min(wallet.debt_credits, credit_count)
        wallet.debt_credits -= debt_offset
        lot = WalletLot(
            wallet_id=wallet.id,
            kind=kind,
            payment_receipt_id=receipt.id if cycle is None else None,
            subscription_cycle_id=cycle and cycle.id,
            granted_credits=credit_count,
            available_credits=credit_count - debt_offset,
            debt_offset_credits=debt_offset,
            expires_at=cycle and cycle.period_end,
        )
        session.add(lot)
        await session.flush()
        self._record_event(session, wallet, lot, receipt, "grant", credit_count)
        if debt_offset:
            self._record_event(
                session,
                wallet,
                lot,
                receipt,
                "debt_offset",
                debt_offset,
            )
        return wallet, lot

    async def _locked_target_wallet(
        self,
        session: AsyncSession,
        intent: PurchaseIntent,
    ) -> Wallet:
        owner = (
            {"user_id": intent.target_user_id}
            if intent.target_user_id is not None
            else {"chat_id": intent.target_chat_id}
        )
        await session.execute(insert(Wallet).values(**owner).on_conflict_do_nothing())
        owner_column = (
            Wallet.user_id if intent.target_user_id is not None else Wallet.chat_id
        )
        owner_id = intent.target_user_id or intent.target_chat_id
        wallet = await session.scalar(
            select(Wallet).where(owner_column == owner_id).with_for_update()
        )
        if wallet is None:  # pragma: no cover - upsert contract
            raise RuntimeError("Target wallet could not be created")
        return wallet

    async def _expire_cycle(
        self,
        session: AsyncSession,
        cycle: SubscriptionCycle,
    ) -> None:
        if cycle.status != "active":
            return
        source = await self._locked_source_lot(
            session,
            WalletLot.subscription_cycle_id == cycle.id,
        )
        cycle.status = "expired"
        if source is None:
            return
        wallet, lot = source
        if lot.available_credits == 0:
            return
        expired = lot.available_credits
        lot.available_credits = 0
        lot.expired_credits += expired
        receipt = await session.get(PaymentReceipt, cycle.payment_receipt_id)
        if receipt is None:
            raise RuntimeError("Allowance payment receipt disappeared")
        self._record_event(
            session,
            wallet,
            lot,
            receipt,
            "expire",
            expired,
        )

    @staticmethod
    async def _locked_source_lot(
        session: AsyncSession,
        source: ColumnElement[bool],
    ) -> tuple[Wallet, WalletLot] | None:
        coordinates = (
            await session.execute(
                select(WalletLot.id, WalletLot.wallet_id).where(source)
            )
        ).one_or_none()
        if coordinates is None:
            return None
        wallet = await session.scalar(
            select(Wallet).where(Wallet.id == coordinates.wallet_id).with_for_update()
        )
        if wallet is None:
            raise RuntimeError("Wallet lot owner disappeared")
        lot = await session.scalar(
            select(WalletLot)
            .where(
                WalletLot.id == coordinates.id,
                WalletLot.wallet_id == wallet.id,
                source,
            )
            .with_for_update()
        )
        if lot is None:
            raise RuntimeError("Wallet lot source changed while locking")
        return wallet, lot

    async def _result_for_receipt(
        self,
        session: AsyncSession,
        receipt: PaymentReceipt,
        *,
        idempotent: bool,
    ) -> FulfillmentResult:
        if receipt.status == "needs_review" or receipt.status == "received":
            return FulfillmentResult(
                receipt.id,
                FulfillmentState.NEEDS_REVIEW,
                intent_id=receipt.purchase_intent_id,
                idempotent=idempotent,
            )
        if receipt.status == "clawed_back":
            result = await self._clawback_result(session, receipt)
            return FulfillmentResult(
                receipt.id,
                FulfillmentState.CLAWED_BACK,
                intent_id=receipt.purchase_intent_id,
                wallet_id=result.wallet_id,
                idempotent=idempotent,
            )
        lot, cycle = await self._lot_and_cycle(session, receipt)
        if lot is None:
            raise RuntimeError("Fulfilled receipt has no wallet lot")
        subscription = (
            await session.get(Subscription, cycle.subscription_id)
            if cycle is not None
            else None
        )
        return FulfillmentResult(
            receipt.id,
            FulfillmentState.FULFILLED,
            intent_id=receipt.purchase_intent_id,
            wallet_id=lot.wallet_id,
            wallet_lot_id=lot.id,
            subscription_id=subscription and subscription.id,
            subscription_cycle_id=cycle and cycle.id,
            available_credits=lot.available_credits,
            debt_offset_credits=lot.debt_offset_credits,
            idempotent=idempotent,
        )

    async def _clawback_result(
        self,
        session: AsyncSession,
        receipt: PaymentReceipt,
    ) -> ClawbackResult:
        lot, _ = await self._lot_and_cycle(session, receipt)
        return ClawbackResult(
            receipt.id,
            lot and lot.wallet_id,
            0,
            0,
            True,
        )

    @staticmethod
    async def _lot_and_cycle(
        session: AsyncSession,
        receipt: PaymentReceipt,
    ) -> tuple[WalletLot | None, SubscriptionCycle | None]:
        lot = await session.scalar(
            select(WalletLot).where(WalletLot.payment_receipt_id == receipt.id)
        )
        if lot is not None:
            return lot, None
        cycle = await session.scalar(
            select(SubscriptionCycle).where(
                SubscriptionCycle.payment_receipt_id == receipt.id
            )
        )
        if cycle is None:
            return None, None
        return (
            await session.scalar(
                select(WalletLot).where(WalletLot.subscription_cycle_id == cycle.id)
            ),
            cycle,
        )

    def _resolve_product(self, intent: PurchaseIntent) -> StarsProduct:
        return self._catalog.resolve(
            ProductKind(intent.product_kind),
            intent.product_id,
            intent.product_version,
        )

    @staticmethod
    def _product_matches(intent: PurchaseIntent, product: StarsProduct) -> bool:
        return (
            intent.product_kind == product.kind.value
            and intent.credits == product.credits
            and intent.stars == product.stars
            and intent.currency == product.currency
            and intent.subscription_period_seconds
            == getattr(product, "period_seconds", None)
        )

    @staticmethod
    def _assert_receipt_matches(
        receipt: PaymentReceipt,
        payment: CapturedPayment,
        payload_hash: str,
    ) -> None:
        expected = (
            payment.provider_charge_id,
            payment.payer_telegram_id,
            payment.currency,
            payment.total_amount,
            payload_hash,
            payment.is_recurring,
            payment.is_first_recurring,
            payment.subscription_expiration_at,
        )
        actual = (
            receipt.provider_charge_id,
            receipt.payer_telegram_id,
            receipt.currency,
            receipt.total_amount,
            receipt.payload_token_hash,
            receipt.is_recurring,
            receipt.is_first_recurring,
            receipt.subscription_expiration_at,
        )
        if actual != expected:
            raise PaymentConflictError(
                "Telegram charge ID replayed with different payment fields"
            )

    @staticmethod
    def _record_event(
        session: AsyncSession,
        wallet: Wallet,
        lot: WalletLot,
        receipt: PaymentReceipt,
        event_type: str,
        amount: int,
    ) -> None:
        session.add(
            WalletLedgerEntry(
                wallet_id=wallet.id,
                wallet_lot_id=lot.id,
                payment_receipt_id=receipt.id,
                event_type=event_type,
                amount_credits=amount,
                available_after=lot.available_credits,
                reserved_after=lot.reserved_credits,
                consumed_after=lot.consumed_credits,
                wallet_debt_after=wallet.debt_credits,
                idempotency_key=f"commerce:{event_type}:{receipt.id}:{lot.id}",
            )
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "PaymentSettlementService clock must return an aware datetime"
            )
        return now


__all__ = ["Clock", "PaymentSettlementService", "TransactionFactory"]
