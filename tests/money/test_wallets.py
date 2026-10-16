import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from derp.db.engine import Database
from derp.money.models import CreditLot, LedgerEntry, Reservation
from derp.money.wallets import (
    balance,
    balances,
    capture,
    claw_back,
    credit,
    expire_lots,
    release,
    release_stale,
    reserve,
    wallet,
)

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
pytestmark = pytest.mark.db


async def assert_ledger_explains_lots(database: Database) -> None:
    """For every lot: amount − remaining == holds − releases + expiries + clawbacks."""
    async with database.session() as session:
        lots = list(await session.scalars(select(CreditLot)))
        for lot in lots:
            sums = {
                row[0]: int(row[1])
                for row in await session.execute(
                    select(LedgerEntry.kind, func.sum(LedgerEntry.amount))
                    .where(LedgerEntry.lot_id == lot.id)
                    .group_by(LedgerEntry.kind)
                )
            }
            taken = (
                sums.get("hold", 0) - sums.get("release", 0) + sums.get("expire", 0) + sums.get("clawback", 0)
            )
            assert sums.get("credit") == lot.amount
            assert lot.amount - lot.remaining == taken, (lot.id, sums)


async def test_credit_and_reserve_are_idempotent(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        assert (await wallet(session, "user", 7)).id == user.id
        assert (await wallet(session, "chat", 7)).id != user.id
        first = await credit(session, user.id, "stars:abc", 650, "purchase")
        again = await credit(session, user.id, "stars:abc", 650, "purchase")
        assert again.id == first.id
        assert await balance(session, user.id) == 650
        held = await reserve(session, user.id, "run:1", 200)
        assert held is not None and held.amount == 200
        assert (await reserve(session, user.id, "run:1", 200)) == held
        assert await balance(session, user.id) == 450
    await assert_ledger_explains_lots(database)


async def test_reserve_refuses_or_takes_what_is_left(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        await credit(session, user.id, "grant:1", 50, "grant")
        assert await reserve(session, user.id, "run:1", 200) is None
        assert await reserve(session, user.id, "run:2", 200, minimum=60) is None
        partial = await reserve(session, user.id, "run:3", 200, minimum=1)
        assert partial is not None and partial.amount == 50
        assert await reserve(session, user.id, "run:4", 10, minimum=1) is None  # empty now
        assert await balance(session, user.id) == 0


async def test_capture_spends_soonest_expiring_first_and_returns_the_rest(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        bought = await credit(session, user.id, "stars:1", 100, "purchase")
        plan = await credit(session, user.id, "plan:1", 100, "plan", expires_at=NOW + timedelta(days=30))
        sponsored = await credit(
            session, user.id, "sponsored:7:2026-10-16", 10, "sponsored", expires_at=NOW + timedelta(hours=12)
        )
        held = await reserve(session, user.id, "run:1", 150, sources=("plan", "purchase"), now=NOW)
        assert held is not None
        assert await balances(session, user.id, now=NOW) == {"purchase": 50, "sponsored": 10}
        assert await capture(session, "run:1", 120, now=NOW) == 120
        assert await capture(session, "run:1", 999, now=NOW) == 120  # repeated: first result
        await session.refresh(bought)
        await session.refresh(plan)
        await session.refresh(sponsored)
        assert (plan.remaining, bought.remaining, sponsored.remaining) == (0, 80, 10)
        await reserve(session, user.id, "run:2", 30, sources=("purchase",), now=NOW)
        assert await capture(session, "run:2", 500, now=NOW) == 30  # never more than held
        await reserve(session, user.id, "run:3", 20, now=NOW)
        await release(session, "run:3", now=NOW)
        assert await balance(session, user.id, now=NOW) == 60
        states = dict((await session.execute(select(Reservation.operation_id, Reservation.state))).all())
        assert states == {"run:1": "captured", "run:2": "captured", "run:3": "released"}
    await assert_ledger_explains_lots(database)


async def test_expired_lots_are_not_spent_and_expire_once(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        await credit(session, user.id, "sponsored:day", 10, "sponsored", expires_at=NOW + timedelta(hours=1))
        await credit(session, user.id, "stars:1", 5, "purchase")
        held = await reserve(session, user.id, "run:1", 12, now=NOW)
        assert held is not None
        later = NOW + timedelta(hours=2)
        assert await balance(session, user.id, now=later) == 3  # only the purchase's unheld rest
        # Returned to a lot that expired meanwhile: those credits expire instead.
        assert await capture(session, "run:1", 2, now=later) == 2
        assert await balance(session, user.id, now=later) == 5
        assert await expire_lots(session, now=later) == 0  # the sponsored lot's hold rest already expired
        await credit(session, user.id, "plan:1", 40, "plan", expires_at=later)
        assert await expire_lots(session, now=later) == 40
        assert await expire_lots(session, now=later) == 0
    await assert_ledger_explains_lots(database)


async def test_claw_back_takes_only_what_is_left(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        await credit(session, user.id, "stars:1", 100, "purchase")
        await reserve(session, user.id, "run:1", 70)
        await capture(session, "run:1", 70)
        assert await claw_back(session, "stars:1", "refund:1") == 30
        assert await claw_back(session, "stars:1", "refund:1") == 30
        assert await claw_back(session, "stars:missing", "refund:2") == 0
        assert await balance(session, user.id) == 0
    await assert_ledger_explains_lots(database)


async def test_concurrent_reservations_never_overspend(database: Database) -> None:
    async with database.transaction() as session:
        wallet_id = (await wallet(session, "chat", -100)).id
        await credit(session, wallet_id, "grant:1", 100, "grant")

    async def attempt(index: int) -> bool:
        async with database.transaction() as session:
            return await reserve(session, wallet_id, f"run:{index}", 30) is not None

    results = await asyncio.gather(*(attempt(i) for i in range(10)))
    assert sum(results) == 3
    async with database.session() as session:
        assert await balance(session, wallet_id) == 10

    async def settle(index: int) -> int:
        async with database.transaction() as session:
            return await capture(session, f"run:{index}", 25)

    held = [i for i, ok in enumerate(results) if ok]
    captured = await asyncio.gather(*(settle(i) for i in held for _ in range(2)))  # duplicates race too
    assert sorted(captured) == [25] * 6
    async with database.session() as session:
        assert await balance(session, wallet_id) == 25
    await assert_ledger_explains_lots(database)


async def test_stale_reservations_are_released(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        await credit(session, user.id, "grant:1", 100, "grant")
        await reserve(session, user.id, "run:1", 60)
        assert await release_stale(session, datetime.now(UTC) - timedelta(minutes=10)) == 0
        assert await release_stale(session, datetime.now(UTC) + timedelta(seconds=1)) == 1
        assert await balance(session, user.id) == 100


async def test_ledger_is_append_only_and_lots_never_negative(database: Database) -> None:
    async with database.transaction() as session:
        user = await wallet(session, "user", 7)
        await credit(session, user.id, "grant:1", 10, "grant")
    with pytest.raises(DBAPIError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(update(LedgerEntry).values(amount=1))
    with pytest.raises(DBAPIError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(text("delete from ledger_entries"))
    with pytest.raises(DBAPIError, match="remaining_in_range"):
        async with database.transaction() as session:
            await session.execute(update(CreditLot).values(remaining=-1))
