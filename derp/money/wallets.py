"""Money movements: credit, reserve → capture/release, expire, claw back.

Every mutation locks the wallet row first, so movements on one wallet are
serial and balances can't go negative under concurrency. Every operation is
keyed by a caller-chosen operation ID (``run:42``, ``stars:<charge id>``…) and
is idempotent: repeating it returns the first result and moves nothing.
Lots are spent soonest-expiring first, then oldest. All functions run inside
the caller's transaction.
"""

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.money.models import CreditLot, Hold, LedgerEntry, Reservation, Wallet


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _spendable(wallet_id: int, now: datetime, sources: Collection[str] | None) -> list[ColumnElement[bool]]:
    conditions = [
        CreditLot.wallet_id == wallet_id,
        CreditLot.remaining > 0,
        or_(CreditLot.expires_at.is_(None), CreditLot.expires_at > now),
    ]
    if sources is not None:
        conditions.append(CreditLot.source.in_(sources))
    return conditions


async def wallet(session: AsyncSession, kind: str, owner_id: int) -> Wallet:
    """The wallet of a user or chat, created on first use."""
    await session.execute(
        insert(Wallet)
        .values(kind=kind, owner_id=owner_id)
        .on_conflict_do_nothing(index_elements=["kind", "owner_id"])
    )
    return (
        await session.execute(select(Wallet).where(Wallet.kind == kind, Wallet.owner_id == owner_id))
    ).scalar_one()


async def _lock(session: AsyncSession, wallet_id: int) -> None:
    await session.execute(select(Wallet.id).where(Wallet.id == wallet_id).with_for_update())


async def balances(session: AsyncSession, wallet_id: int, *, now: datetime | None = None) -> dict[str, int]:
    """Spendable credits per lot source; held credits are not included."""
    rows = await session.execute(
        select(CreditLot.source, func.sum(CreditLot.remaining))
        .where(*_spendable(wallet_id, _now(now), None))
        .group_by(CreditLot.source)
    )
    return {row[0]: int(row[1]) for row in rows}


async def balance(
    session: AsyncSession,
    wallet_id: int,
    *,
    sources: Collection[str] | None = None,
    now: datetime | None = None,
) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(CreditLot.remaining), 0)).where(
            *_spendable(wallet_id, _now(now), sources)
        )
    )
    return int(total or 0)


async def credit(
    session: AsyncSession,
    wallet_id: int,
    operation_id: str,
    amount: int,
    source: str,
    *,
    expires_at: datetime | None = None,
) -> CreditLot:
    """Add a lot; repeating the operation returns the existing lot."""
    await _lock(session, wallet_id)
    if existing := await session.scalar(select(CreditLot).where(CreditLot.operation_id == operation_id)):
        return existing
    lot = CreditLot(
        wallet_id=wallet_id,
        operation_id=operation_id,
        source=source,
        amount=amount,
        remaining=amount,
        expires_at=expires_at,
    )
    session.add(lot)
    await session.flush()
    session.add(
        LedgerEntry(
            wallet_id=wallet_id, lot_id=lot.id, operation_id=operation_id, kind="credit", amount=amount
        )
    )
    await session.flush()
    return lot


async def reserve(
    session: AsyncSession,
    wallet_id: int,
    operation_id: str,
    amount: int,
    *,
    sources: Collection[str] | None = None,
    minimum: int | None = None,
    now: datetime | None = None,
) -> Reservation | None:
    """Hold up to ``amount`` credits from the given lot sources.

    Holds the full amount, or — when ``minimum`` is given — whatever is
    available as long as it reaches ``minimum``. Returns None when the wallet
    can't cover it. Repeating the operation returns the original reservation.
    """
    await _lock(session, wallet_id)
    if existing := await session.scalar(select(Reservation).where(Reservation.operation_id == operation_id)):
        return existing
    lots = list(
        await session.scalars(
            select(CreditLot)
            .where(*_spendable(wallet_id, _now(now), sources))
            .order_by(CreditLot.expires_at.asc().nulls_last(), CreditLot.id)
        )
    )
    available = sum(lot.remaining for lot in lots)
    take = min(amount, available)
    if take <= 0 or take < (amount if minimum is None else minimum):
        return None
    reservation = Reservation(wallet_id=wallet_id, operation_id=operation_id, amount=take)
    session.add(reservation)
    await session.flush()
    left = take
    for lot in lots:
        if left == 0:
            break
        part = min(lot.remaining, left)
        lot.remaining -= part
        left -= part
        session.add(Hold(reservation_id=reservation.id, lot_id=lot.id, amount=part))
        session.add(
            LedgerEntry(
                wallet_id=wallet_id, lot_id=lot.id, operation_id=operation_id, kind="hold", amount=part
            )
        )
    await session.flush()
    return reservation


async def capture(
    session: AsyncSession, operation_id: str, amount: int, *, now: datetime | None = None
) -> int:
    """Spend ``amount`` of a reservation and return the rest to its lots.

    The charge never exceeds what was held (the difference is eaten, never
    owed). Returns the credits actually captured; repeating returns the first
    result. Credits returned to a lot that expired meanwhile are expired.
    """
    reservation = await session.scalar(select(Reservation).where(Reservation.operation_id == operation_id))
    if reservation is None:
        raise LookupError("no such reservation")
    await _lock(session, reservation.wallet_id)
    await session.refresh(reservation)
    if reservation.state != "held":
        return reservation.captured
    moment = _now(now)
    holds = (
        await session.execute(
            select(Hold, CreditLot)
            .join(CreditLot, CreditLot.id == Hold.lot_id)
            .where(Hold.reservation_id == reservation.id)
            .order_by(CreditLot.expires_at.asc().nulls_last(), CreditLot.id)
        )
    ).all()
    spend = max(0, min(amount, reservation.amount))
    wallet_id = reservation.wallet_id

    def entry(lot: CreditLot, kind: str, part: int) -> None:
        session.add(
            LedgerEntry(wallet_id=wallet_id, lot_id=lot.id, operation_id=operation_id, kind=kind, amount=part)
        )

    left = spend
    for row in holds:
        hold, lot = row[0], row[1]
        used = min(hold.amount, left)
        left -= used
        if used:
            entry(lot, "capture", used)
        if back := hold.amount - used:
            entry(lot, "release", back)
            if lot.expires_at is not None and lot.expires_at <= moment:
                entry(lot, "expire", back)
            else:
                lot.remaining += back
    reservation.captured = spend
    reservation.state = "captured" if spend else "released"
    reservation.settled_at = moment
    await session.flush()
    return spend


async def release(session: AsyncSession, operation_id: str, *, now: datetime | None = None) -> None:
    """Return a whole reservation (the operation failed before costing anything)."""
    await capture(session, operation_id, 0, now=now)


async def release_stale(session: AsyncSession, before: datetime, *, now: datetime | None = None) -> int:
    """Release reservations still held since before ``before`` (their process died)."""
    stale = list(
        await session.scalars(
            select(Reservation.operation_id).where(
                Reservation.state == "held", Reservation.created_at < before
            )
        )
    )
    for operation_id in stale:
        await release(session, operation_id, now=now)
    return len(stale)


async def expire_lots(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Zero out lots past their expiry; returns the credits expired."""
    moment = _now(now)
    wallet_ids = list(
        await session.scalars(
            select(CreditLot.wallet_id)
            .where(CreditLot.remaining > 0, CreditLot.expires_at <= moment)
            .distinct()
            .order_by(CreditLot.wallet_id)
        )
    )
    total = 0
    for wallet_id in wallet_ids:
        await _lock(session, wallet_id)
        lots = await session.scalars(
            select(CreditLot).where(
                CreditLot.wallet_id == wallet_id, CreditLot.remaining > 0, CreditLot.expires_at <= moment
            )
        )
        for lot in lots:
            session.add(
                LedgerEntry(
                    wallet_id=wallet_id,
                    lot_id=lot.id,
                    operation_id=f"expire:{lot.id}",
                    kind="expire",
                    amount=lot.remaining,
                )
            )
            total += lot.remaining
            lot.remaining = 0
    await session.flush()
    return total


async def claw_back(session: AsyncSession, lot_operation_id: str, operation_id: str) -> int:
    """Remove what's left of a lot (a refunded purchase); returns the credits taken.

    Spent or held credits are not recovered: there is no debt.
    """
    lot = await session.scalar(select(CreditLot).where(CreditLot.operation_id == lot_operation_id))
    if lot is None:
        return 0
    await _lock(session, lot.wallet_id)
    await session.refresh(lot)
    done = await session.scalar(
        select(LedgerEntry.amount).where(
            LedgerEntry.operation_id == operation_id,
            LedgerEntry.kind == "clawback",
            LedgerEntry.lot_id == lot.id,
        )
    )
    if done is not None:
        return done
    taken = lot.remaining
    if taken:
        session.add(
            LedgerEntry(
                wallet_id=lot.wallet_id,
                lot_id=lot.id,
                operation_id=operation_id,
                kind="clawback",
                amount=taken,
            )
        )
        lot.remaining = 0
        await session.flush()
    return taken
