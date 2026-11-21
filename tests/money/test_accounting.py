from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from derp.agent.models import AgentRun, RunOutput
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, ChatMember, User
from derp.inference.models import ProviderCall
from derp.money.accounting import date_range, message_ref, receipts, usage, wallet_snapshot
from derp.money.wallets import capture, credit, reserve, wallet
from tests.keys.test_keys_db import add_key

CHAT = -1004242
NOW = datetime(2026, 3, 29, 10, tzinfo=UTC)


def test_receipts_validate_permalinks_and_local_days() -> None:
    chat = Chat(id=CHAT, type="supergroup", username="turtles")
    for ref in ["#19", "19", "https://t.me/c/4242/19", "https://t.me/turtles/12/19?t=2"]:
        assert message_ref(chat, ref) == 19
    for ref in ["https://t.me/c/4243/19", "https://t.me/fish/19", "https://t.me.evil.test/turtles/19", "0"]:
        assert message_ref(chat, ref) is None
    first, last = date_range(None, None, NOW, ZoneInfo("Europe/London"))
    assert last.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=23)


@pytest.mark.db
async def test_accounting_counts_shared_settlements_calls_and_screening_once(database: Database) -> None:
    async with database.transaction() as s:
        chat = Chat(id=CHAT, type="supergroup")
        s.add_all([chat, User(id=1, first_name="Ann")])
        await s.flush()
        s.add(ChatMember(chat_id=CHAT, user_id=1, status="member"))
        shared = await wallet(s, "chat", CHAT)
        await credit(s, shared.id, "grant", 20, "grant", expires_at=None)
        screen = await reserve(s, shared.id, "screen", Decimal("0.1"))
        assert screen is not None
        await capture(s, "screen", Decimal("0.0234"))
        await reserve(s, shared.id, "turn", 5)
        run = AgentRun(
            chat_id=CHAT,
            user_id=1,
            trigger="message",
            status="completed",
            role="chat",
            funding="paid",
            wallet_id=shared.id,
            finished_at=NOW,
            served_model="blue",
            charged_credits=Decimal("1.234"),
            created_at=NOW,
        )
        s.add(run)
        await s.flush()
        await capture(s, "turn", Decimal("1.234"))
        s.add(RunOutput(run_id=run.id, chat_id=CHAT, message_id=19, kind="message"))
        # A separate chat can receive a tool delivery; its members cannot inspect the originating run.
        s.add(RunOutput(run_id=run.id, chat_id=55, message_id=19, kind="message"))
        for kind, model, n in [("image", "picture", 2), ("chat", "blue", 70)]:
            s.add(
                ProviderCall(
                    kind=kind,
                    run_id=run.id,
                    chat_id=CHAT,
                    user_id=1,
                    model=model,
                    funding="paid",
                    input_tokens=n,
                    cost_usd=Decimal("0.0005"),
                    created_at=NOW,
                )
            )
        s.add(
            ProviderCall(
                kind="decision",
                role="rule_screen",
                chat_id=CHAT,
                user_id=1,
                model="jev",
                reservation_id=screen.id,
                funding="paid",
                input_tokens=123,
                cost_usd=Decimal("0.0000195"),
                created_at=NOW,
            )
        )
        s.add(
            ProviderCall(
                kind="chat", chat_id=55, model="other", funding="paid", input_tokens=9999, created_at=NOW
            )
        )
        # Multiple provider calls may belong to a single standalone reservation.
        s.add(
            ProviderCall(
                kind="decision",
                role="rule_screen",
                chat_id=CHAT,
                user_id=1,
                model="jev",
                reservation_id=screen.id,
                funding="paid",
                input_tokens=5,
                created_at=NOW,
            )
        )
        await reserve(s, shared.id, "other-hold", Decimal("0.5"))
    async with database.session() as s:
        first, last = date_range(date(2026, 3, 29), None, NOW, ZoneInfo("Europe/London"))
        snapshot = await wallet_snapshot(s, chat, 1, NOW)
        assert (snapshot["available_dc"], snapshot["held_dc"]) == ("18.2426", "0.5")
        for by in ("model", "call_type", "day", "key_contributor", "funding", "member", "total"):
            report = await usage(s, chat, first, last, group_by=by)
            assert report["totals"]["input_tokens"] == 200
            assert report["totals"]["calls"] == 4 and report["totals"]["wallet_dc"] == "1.2574"
        report = await usage(s, chat, first, last, member="Ann", call_type="screening")
        assert report["totals"]["wallet_dc"] == "0.0234"
        assert (await usage(s, chat, first, last, model="picture"))["totals"]["calls"] == 1
        assert (await receipts(s, CHAT, 19))[0].provider_usd == Decimal("0.001")
        assert not await receipts(s, 55, 19)


@pytest.mark.db
async def test_key_spend_groups_respect_anonymous_patrons(database: Database, settings: Settings) -> None:
    keyed = settings.model_copy(update={"keys_secret": "test-secret"})
    async with database.transaction() as s:
        chat = Chat(id=CHAT, type="supergroup")
        s.add_all(
            [chat, User(id=9, first_name="Private", patron_named=False), User(id=11, first_name="Visible")]
        )
    for owner in (9, 11):
        key = await add_key(database, keyed, owner, chats=(CHAT,))
        async with database.transaction() as s:
            s.add(
                ProviderCall(
                    kind="chat",
                    chat_id=CHAT,
                    funding="key",
                    model="m",
                    key_id=key,
                    upstream_usd=Decimal("0.005"),
                    created_at=NOW,
                )
            )
    async with database.session() as s:
        start, stop = date_range(None, None, NOW, ZoneInfo("Europe/London"))
        report = await usage(s, chat, start, stop, group_by="key_contributor")
    names = {row["key_contributor"] for row in report["groups_top_50"]}
    assert names == {"anonymous patron", "Visible"} and report["totals"]["key_usd"] == "0.01"
