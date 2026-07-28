"""Transaction-local provenance for payment debt and its repayment."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.models import (
    PaymentReceipt,
    SubscriptionCycle,
    Wallet,
    WalletDebtRepaymentAllocation,
    WalletDebtSource,
    WalletLot,
)


class WalletDebtProvenanceError(RuntimeError):
    """Stored wallet debt cannot be reconciled to its durable sources."""


@dataclass(frozen=True, slots=True)
class DebtRepayment:
    """One grant-lot allocation that paid an exact debt source."""

    debt_source_id: uuid.UUID
    source_wallet_lot_id: uuid.UUID
    repayment_wallet_lot_id: uuid.UUID
    credits: int


@dataclass(frozen=True, slots=True)
class DebtRecovery:
    """Debt eliminated directly plus repaid entitlement restored to grant lots."""

    direct_credits: int
    restorations: tuple[DebtRepayment, ...]

    @property
    def total_credits(self) -> int:
        return self.direct_credits + sum(item.credits for item in self.restorations)


class WalletDebtProvenance:
    """Maintain exact debt sources under an already locked wallet row."""

    @staticmethod
    async def incur(
        session: AsyncSession,
        wallet: Wallet,
        source_lot: WalletLot,
        amount_credits: int,
    ) -> WalletDebtSource | None:
        """Create the one debt source for a clawed-back lot."""
        WalletDebtProvenance._require_positive_or_zero(amount_credits)
        if amount_credits == 0:
            return None
        existing = await session.scalar(
            select(WalletDebtSource)
            .where(WalletDebtSource.source_wallet_lot_id == source_lot.id)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.wallet_id != wallet.id
                or existing.incurred_credits != amount_credits
            ):
                raise WalletDebtProvenanceError(
                    "Debt source was replayed with new terms"
                )
            return existing
        source = WalletDebtSource(
            wallet_id=wallet.id,
            source_wallet_lot_id=source_lot.id,
            incurred_credits=amount_credits,
            outstanding_credits=amount_credits,
        )
        session.add(source)
        wallet.debt_credits += amount_credits
        await session.flush([source])
        await WalletDebtProvenance._assert_source_reconciled(session, source)
        await WalletDebtProvenance._assert_all_debt_reconciled(session, wallet)
        return source

    @staticmethod
    async def repay(
        session: AsyncSession,
        wallet: Wallet,
        repayment_lot: WalletLot,
        credit_limit: int,
    ) -> tuple[DebtRepayment, ...]:
        """Apply a new lot to open sources in deterministic FIFO order."""
        WalletDebtProvenance._require_positive_or_zero(credit_limit)
        sources = list(
            await session.scalars(
                select(WalletDebtSource)
                .where(
                    WalletDebtSource.wallet_id == wallet.id,
                    WalletDebtSource.outstanding_credits > 0,
                )
                .order_by(WalletDebtSource.created_at, WalletDebtSource.id)
                .with_for_update()
            )
        )
        WalletDebtProvenance._assert_wallet_debt(wallet, sources)
        remaining = min(wallet.debt_credits, credit_limit)
        repayments: list[DebtRepayment] = []
        for source in sources:
            if remaining == 0:
                break
            allocated = min(source.outstanding_credits, remaining)
            source.outstanding_credits -= allocated
            wallet.debt_credits -= allocated
            repayment_lot.available_credits -= allocated
            repayment_lot.debt_offset_credits += allocated
            session.add(
                WalletDebtRepaymentAllocation(
                    debt_source_id=source.id,
                    repayment_wallet_lot_id=repayment_lot.id,
                    wallet_id=wallet.id,
                    allocated_credits=allocated,
                )
            )
            repayments.append(
                DebtRepayment(
                    source.id,
                    source.source_wallet_lot_id,
                    repayment_lot.id,
                    allocated,
                )
            )
            remaining -= allocated
        if remaining:
            raise WalletDebtProvenanceError("Wallet debt has no durable source")
        for source in sources:
            await WalletDebtProvenance._assert_source_reconciled(session, source)
        return tuple(repayments)

    @staticmethod
    async def reopen_repayments(
        session: AsyncSession,
        wallet: Wallet,
        repayment_lot: WalletLot,
    ) -> tuple[DebtRepayment, ...]:
        """Undo active repayments when their grant lot is itself clawed back."""
        rows = list(
            (
                await session.execute(
                    select(WalletDebtRepaymentAllocation, WalletDebtSource)
                    .join(
                        WalletDebtSource,
                        WalletDebtSource.id
                        == WalletDebtRepaymentAllocation.debt_source_id,
                    )
                    .where(
                        WalletDebtRepaymentAllocation.wallet_id == wallet.id,
                        WalletDebtRepaymentAllocation.repayment_wallet_lot_id
                        == repayment_lot.id,
                        WalletDebtRepaymentAllocation.restored_credits
                        + WalletDebtRepaymentAllocation.revoked_credits
                        < WalletDebtRepaymentAllocation.allocated_credits,
                    )
                    .order_by(
                        WalletDebtRepaymentAllocation.created_at,
                        WalletDebtRepaymentAllocation.debt_source_id,
                    )
                    .with_for_update(
                        of=(WalletDebtRepaymentAllocation, WalletDebtSource)
                    )
                )
            ).tuples()
        )
        reopened: list[DebtRepayment] = []
        for allocation, source in rows:
            reopened_credits = (
                allocation.allocated_credits
                - allocation.restored_credits
                - allocation.revoked_credits
            )
            allocation.revoked_credits += reopened_credits
            source.outstanding_credits += reopened_credits
            repayment_lot.debt_offset_credits -= reopened_credits
            repayment_lot.clawed_back_credits += reopened_credits
            wallet.debt_credits += reopened_credits
            reopened.append(
                DebtRepayment(
                    source.id,
                    source.source_wallet_lot_id,
                    repayment_lot.id,
                    reopened_credits,
                )
            )
            await WalletDebtProvenance._assert_source_reconciled(session, source)
        await WalletDebtProvenance._assert_all_debt_reconciled(session, wallet)
        return tuple(reopened)

    @staticmethod
    async def recover(
        session: AsyncSession,
        wallet: Wallet,
        source_lot: WalletLot,
        amount_credits: int,
        now: datetime,
    ) -> DebtRecovery:
        """Retire source debt, restoring repaying lots only after open debt clears."""
        WalletDebtProvenance._require_positive_or_zero(amount_credits)
        source = await session.scalar(
            select(WalletDebtSource)
            .where(
                WalletDebtSource.wallet_id == wallet.id,
                WalletDebtSource.source_wallet_lot_id == source_lot.id,
            )
            .with_for_update()
        )
        if source is None:
            raise WalletDebtProvenanceError("Clawed-back inventory has no debt source")

        direct = min(source.outstanding_credits, amount_credits)
        source.outstanding_credits -= direct
        source.recovered_credits += direct
        wallet.debt_credits -= direct
        remaining = amount_credits - direct
        restorations: list[DebtRepayment] = []
        if remaining:
            rows = list(
                (
                    await session.execute(
                        select(WalletDebtRepaymentAllocation, WalletLot)
                        .join(
                            WalletLot,
                            WalletLot.id
                            == WalletDebtRepaymentAllocation.repayment_wallet_lot_id,
                        )
                        .where(
                            WalletDebtRepaymentAllocation.debt_source_id == source.id,
                            WalletDebtRepaymentAllocation.restored_credits
                            + WalletDebtRepaymentAllocation.revoked_credits
                            < WalletDebtRepaymentAllocation.allocated_credits,
                        )
                        .order_by(
                            WalletDebtRepaymentAllocation.created_at,
                            WalletDebtRepaymentAllocation.repayment_wallet_lot_id,
                        )
                        .with_for_update(of=(WalletDebtRepaymentAllocation, WalletLot))
                    )
                ).tuples()
            )
            for allocation, repayment_lot in rows:
                if remaining == 0:
                    break
                repayment_was_clawed_back = (
                    await WalletDebtProvenance._source_was_clawed_back(
                        session, repayment_lot
                    )
                )
                if repayment_was_clawed_back:
                    raise WalletDebtProvenanceError(
                        "Clawed-back lot retained an active debt repayment"
                    )
                active = (
                    allocation.allocated_credits
                    - allocation.restored_credits
                    - allocation.revoked_credits
                )
                restored = min(active, remaining)
                allocation.restored_credits += restored
                source.recovered_credits += restored
                repayment_lot.debt_offset_credits -= restored
                if (
                    repayment_lot.expires_at is not None
                    and repayment_lot.expires_at <= now
                ):
                    repayment_lot.expired_credits += restored
                else:
                    repayment_lot.available_credits += restored
                restorations.append(
                    DebtRepayment(
                        source.id,
                        source.source_wallet_lot_id,
                        repayment_lot.id,
                        restored,
                    )
                )
                remaining -= restored
        if remaining:
            raise WalletDebtProvenanceError("Debt recovery exceeds its open provenance")
        await WalletDebtProvenance._assert_source_reconciled(session, source)
        await WalletDebtProvenance._assert_all_debt_reconciled(session, wallet)
        return DebtRecovery(direct, tuple(restorations))

    @staticmethod
    async def _assert_source_reconciled(
        session: AsyncSession, source: WalletDebtSource
    ) -> None:
        active_repayments = int(
            await session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            WalletDebtRepaymentAllocation.allocated_credits
                            - WalletDebtRepaymentAllocation.restored_credits
                            - WalletDebtRepaymentAllocation.revoked_credits
                        ),
                        0,
                    )
                ).where(WalletDebtRepaymentAllocation.debt_source_id == source.id)
            )
            or 0
        )
        if source.incurred_credits != (
            source.outstanding_credits + source.recovered_credits + active_repayments
        ):
            raise WalletDebtProvenanceError("Debt source counters do not reconcile")

    @staticmethod
    async def _assert_all_debt_reconciled(
        session: AsyncSession, wallet: Wallet
    ) -> None:
        outstanding = int(
            await session.scalar(
                select(
                    func.coalesce(func.sum(WalletDebtSource.outstanding_credits), 0)
                ).where(WalletDebtSource.wallet_id == wallet.id)
            )
            or 0
        )
        if wallet.debt_credits != outstanding:
            raise WalletDebtProvenanceError("Wallet debt does not match open sources")

    @staticmethod
    def _assert_wallet_debt(wallet: Wallet, sources: list[WalletDebtSource]) -> None:
        if wallet.debt_credits != sum(item.outstanding_credits for item in sources):
            raise WalletDebtProvenanceError("Wallet debt does not match open sources")

    @staticmethod
    async def _source_was_clawed_back(session: AsyncSession, lot: WalletLot) -> bool:
        if lot.payment_receipt_id is not None:
            return (
                await session.scalar(
                    select(PaymentReceipt.status).where(
                        PaymentReceipt.id == lot.payment_receipt_id
                    )
                )
                == "clawed_back"
            )
        if lot.subscription_cycle_id is not None:
            return (
                await session.scalar(
                    select(SubscriptionCycle.status).where(
                        SubscriptionCycle.id == lot.subscription_cycle_id
                    )
                )
                == "clawed_back"
            )
        return False

    @staticmethod
    def _require_positive_or_zero(amount_credits: int) -> None:
        if (
            isinstance(amount_credits, bool)
            or not isinstance(amount_credits, int)
            or amount_credits < 0
        ):
            raise ValueError("credits must be a non-negative integer")


__all__ = [
    "DebtRecovery",
    "DebtRepayment",
    "WalletDebtProvenance",
    "WalletDebtProvenanceError",
]
