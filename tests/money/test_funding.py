from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from derp.agent.runs import start_run
from derp.config import Settings
from derp.db.engine import Database
from derp.inference.models import ModelPin, ProviderCall
from derp.money.funding import Funder, Funding, Funds
from derp.money.models import CreditLot, Reservation
from derp.money.wallets import balance, balances, credit, wallet

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
pytestmark = pytest.mark.db


async def give(database: Database, kind: str, owner: int, amount: int, op: str) -> int:
    async with database.transaction() as session:
        wallet_id = (await wallet(session, kind, owner)).id
        await credit(session, wallet_id, op, amount, "purchase")
    return wallet_id


async def test_private_runs_reserve_up_to_the_run_cap_from_paid_credits(
    database: Database, settings: Settings
) -> None:
    funder = Funder(database, settings)
    mine = await give(database, "user", 7, 500, "stars:1")
    paid = await funder.reserve(chat_id=7, private=True, user_id=7, operation_id="turn:1", now=NOW)
    assert paid is not None and (paid.funding, paid.wallet_id, paid.credits) == (Funding.PAID, mine, 200)
    async with database.session() as session:
        assert await balances(session, mine, now=NOW) == {"purchase": 300}  # no sponsored lot needed

    async with database.transaction() as session:
        await credit(session, (await wallet(session, "user", 8)).id, "grant:dust", 1, "grant")
    dust = await funder.reserve(chat_id=8, private=True, user_id=8, operation_id="turn:2", now=NOW)
    assert dust is not None and (dust.funding, dust.credits) == (
        Funding.PAID,
        1,
    )  # no prices: any credit will do
    async with database.transaction() as session:
        lot = await session.scalar(select(CreditLot).where(CreditLot.operation_id == "grant:dust"))
        assert lot is not None and lot.remaining == 0


async def test_paid_credits_below_a_typical_turn_fall_through_to_sponsorship(
    database: Database, settings: Settings
) -> None:
    async with database.transaction() as session:
        # $10/M prompt tokens: half the 16k paid window is ≈ $0.08 ≈ 100 credits
        session.add(ModelPin(role="chat", pin={}, price_prompt=Decimal(10), price_completion=Decimal(10)))
    funder = Funder(database, settings)
    mine = await give(database, "user", 7, 50, "stars:1")
    funds = await funder.reserve(chat_id=7, private=True, user_id=7, operation_id="turn:1", now=NOW)
    assert funds is not None and funds.funding is Funding.SPONSORED
    assert funds.credits == settings.sponsored_daily_credits
    async with database.session() as session:
        assert await balance(session, mine, sources=("purchase",), now=NOW) == 50
        assert await balance(session, mine, sources=("sponsored",), now=NOW) == 0
    assert await funder.reserve(chat_id=7, private=True, user_id=7, operation_id="turn:2", now=NOW) is None


async def test_a_typical_turn_counts_the_prompt_prefix(database: Database, settings: Settings) -> None:
    async with database.transaction() as session:
        # $10/M: half the 16k window + 300 out ≈ 100 credits; with an 8k persona + tools prefix ≈ 196
        session.add(ModelPin(role="chat", pin={}, price_prompt=Decimal(10), price_completion=Decimal(10)))
    await give(database, "user", 7, 150, "stars:1")
    await give(database, "user", 8, 150, "stars:2")
    bare = await Funder(database, settings).reserve(
        chat_id=7, private=True, user_id=7, operation_id="turn:1", now=NOW
    )
    assert bare is not None and bare.funding is Funding.PAID
    prefixed = Funder(database, settings, prefix={Funding.PAID: 8_000})
    async with database.session() as session:
        assert await prefixed.typical_turn(session, Funding.PAID) == 196
    funds = await prefixed.reserve(chat_id=8, private=True, user_id=8, operation_id="turn:2", now=NOW)
    assert funds is not None and funds.funding is Funding.SPONSORED  # 150 can't cover a real turn


async def test_groups_spend_chat_credits_then_the_invokers(database: Database, settings: Settings) -> None:
    funder = Funder(database, settings)
    chat = await give(database, "chat", -100, 250, "stars:chat")
    mine = await give(database, "user", 7, 100, "stars:me")
    first = await funder.reserve(chat_id=-100, private=False, user_id=7, operation_id="turn:1", now=NOW)
    second = await funder.reserve(chat_id=-100, private=False, user_id=7, operation_id="turn:2", now=NOW)
    third = await funder.reserve(chat_id=-100, private=False, user_id=7, operation_id="turn:3", now=NOW)
    fourth = await funder.reserve(chat_id=-100, private=False, user_id=7, operation_id="turn:4", now=NOW)
    assert first is not None and (first.wallet_id, first.credits) == (chat, 200)
    assert second is not None and (second.wallet_id, second.credits) == (chat, 50)
    assert third is not None and (third.wallet_id, third.credits, third.funding) == (mine, 100, Funding.PAID)
    assert fourth is not None and (fourth.wallet_id, fourth.funding) == (mine, Funding.SPONSORED)


async def test_the_global_sponsored_cap_stops_sponsorship_and_pings_once(
    database: Database, settings: Settings
) -> None:
    pings: list[str] = []

    async def notify(text: str) -> None:
        pings.append(text)

    funder = Funder(database, settings, notify=notify)
    async with database.transaction() as session:
        session.add(
            ProviderCall(kind="chat", model="m", funding="sponsored", cost_usd=Decimal("1.0"), created_at=NOW)
        )
    assert await funder.reserve(chat_id=7, private=True, user_id=7, operation_id="turn:1", now=NOW) is None
    assert await funder.reserve(chat_id=8, private=True, user_id=8, operation_id="turn:2", now=NOW) is None
    assert len(pings) == 1 and "cap" in pings[0]


async def test_settle_charges_marked_up_run_cost_only_when_delivered(
    database: Database, settings: Settings
) -> None:
    funder = Funder(database, settings)
    mine = await give(database, "user", 7, 500, "stars:1")
    funds = [
        await funder.reserve(chat_id=7, private=True, user_id=7, operation_id=f"turn:{i}") for i in (1, 2)
    ]
    async with database.transaction() as session:
        run = await start_run(session, chat_id=7, user_id=7, trigger="message", role="chat", funding="paid")
        for cost in ("0.01", "0.0042"):  # model turn + an image note: ceil(0.0142 × 1.2 / 0.001) = 18
            session.add(
                ProviderCall(kind="chat", model="m", funding="paid", run_id=run.id, cost_usd=Decimal(cost))
            )
    held: list[Funds] = [f for f in funds if f is not None]
    async with database.transaction() as session:
        assert await funder.settle(session, held[0], run.id, delivered=True) == 18
        assert await funder.settle(session, held[1], run.id, delivered=False) == 0
    async with database.session() as session:
        assert await balance(session, mine) == 500 - 18
        states = dict((await session.execute(select(Reservation.operation_id, Reservation.state))).all())
        assert states == {"turn:1": "captured", "turn:2": "released"}
