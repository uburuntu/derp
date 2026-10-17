from datetime import UTC, datetime, timedelta

import pytest

from derp.agent.models import AgentRun
from derp.billing.patrons import runway
from derp.db.engine import Database
from derp.money.wallets import wallet

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
CHAT = -5001
pytestmark = pytest.mark.db


async def spend(database: Database, wallet_id: int, *days_ago: int, credits: int = 100) -> None:
    async with database.transaction() as session:
        for ago in days_ago:
            session.add(
                AgentRun(
                    chat_id=CHAT, user_id=1, trigger="message", role="chat", funding="paid", wallet_id=wallet_id,
                    charged_credits=credits, status="completed", created_at=NOW - timedelta(days=ago, hours=1),
                )
            )  # fmt: skip


async def line(database: Database, wallet_id: int, left: int, turn: int = 10) -> str:
    async with database.session() as session:
        return await runway(session, "en", wallet_id, left, turn, NOW)


async def test_runway_follows_the_chats_recent_pace(database: Database) -> None:
    async with database.transaction() as session:
        wallet_id = (await wallet(session, "chat", CHAT)).id
    # Too little history: answers at a typical turn's price.
    await spend(database, wallet_id, 0, 1)
    assert await line(database, wallet_id, 1300) == "Wallet: **1,300** dc — ≈ **130** answers."
    assert await line(database, wallet_id, 5) == "Wallet: **5** dc."
    # Three active days over the last three days: 300 dc spent, 100 a day.
    await spend(database, wallet_id, 2)
    assert await line(database, wallet_id, 1300) == "Wallet: **1,300** dc — about **13 days** at your pace."
    assert await line(database, wallet_id, 150) == "Wallet: **150** dc — about a day at your pace."
    assert await line(database, wallet_id, 50) == "Wallet: **50** dc — less than a day at your pace."
    assert await line(database, wallet_id, 9750) == "Wallet: **9,750** dc — over a month at your pace."
    # Spending older than the pace window doesn't count; quiet days within it dilute the pace.
    await spend(database, wallet_id, 20, credits=10_000)
    await spend(database, wallet_id, 9)
    # 400 dc over the ten days since the first spend in the window: 40 a day.
    assert await line(database, wallet_id, 1000) == "Wallet: **1,000** dc — about **25 days** at your pace."
