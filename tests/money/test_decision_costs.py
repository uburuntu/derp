"""One group rule check, from its typed HTTP request through its wallet or contributed-key ledger."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from derp.agent.decisions import Decider
from derp.agent.runs import start_run
from derp.config import Decisions, Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.inference.api import OpenRouterAPI, estimated_tokens
from derp.inference.models import ProviderCall
from derp.keys.models import ContributedKey
from derp.memory.models import ReplyRule
from derp.money.funding import Funder, Funding, Funds
from derp.money.models import Reservation
from derp.money.wallets import balance, credit, reserve, wallet
from tests.agent.test_decisions import CHAT, UTC_TZ, pins, seed, tg
from tests.keys.test_keys_db import add_key

pytestmark = pytest.mark.db


async def group_rule(database: Database) -> tuple[Chat, ReplyRule, int, int]:
    await seed(database)
    async with database.transaction() as session:
        chat = await session.get(Chat, CHAT)
        assert chat is not None
        rule = ReplyRule(
            chat_id=CHAT,
            admin_id=2,
            state="active",
            condition="A dog is mentioned",
            action="Ask about its breed",
        )
        session.add(rule)
        shared = await wallet(session, "chat", CHAT)
        person = await wallet(session, "user", 1)
        await credit(session, person.id, "personal", 20, "purchase")
        return chat, rule, shared.id, person.id


@pytest.mark.parametrize(
    "outcome", ["match", "miss", "bad_answers", "bad_value", "bad_json", "bad_usage", "timeout", "refused"]
)
async def test_wallet_settles_screens_at_provider_cost_even_without_a_match(
    database: Database, settings: Settings, outcome: str
) -> None:
    chat, rule, shared, person = await group_rule(database)
    config = settings.model_copy(update={"decisions": Decisions(timeout_seconds=0.1)})
    async with database.transaction() as s:
        await credit(s, shared, "grant", 2, "grant")
    requests: list[httpx.Request] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if outcome == "timeout":
            await asyncio.Event().wait()
        if outcome == "refused":
            return httpx.Response(503)
        if outcome == "bad_json":
            return httpx.Response(200, content=b"{")
        if outcome == "bad_usage":
            return httpx.Response(200, json={"usage": "unreadable"})
        value = "unknown" if outcome == "bad_value" else (0.81 if outcome == "match" else 0.2)
        answers = {} if outcome == "bad_answers" else {f"rule_{rule.id}": {"type": "noul", "noul": value}}
        return httpx.Response(
            200,
            json={
                "answers": answers,
                "usage": {
                    "input_tokens": 123,
                    "input_tokens_details": {"cached_tokens": 11},
                    "cost": "0.00001234",
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/", transport=httpx.MockTransport(provider)
    ) as client:
        decider = Decider(database, OpenRouterAPI(config, client), await pins(database, config), config)
        message = tg(8, "a long message " * 230 + "new dog").model_copy(
            update={"reply_to_message": tg(7, "an unstored reply must stay private")}
        )
        result = await decider.match_rules(message, chat=chat, candidates=[rule], bot_id=42)
    assert result == ({rule.id} if outcome == "match" else set())
    [request] = requests
    state = json.loads(request.content)["state"]
    assert state["new_message"].endswith("new dog") and "reply_to" not in state
    async with database.session() as s:
        held = (await s.scalars(select(Reservation))).one()
        assert held.wallet_id == shared
        call = await s.scalar(select(ProviderCall))
        if outcome == "refused":
            assert held.state == "released" and held.captured == 0 and call is None
        else:
            assert call is not None and call.reservation_id == held.id
            if outcome in {"timeout", "bad_json", "bad_usage"}:
                tokens = estimated_tokens(json.loads(request.content), photo=False)
                expected_usd = (Decimal("0.042") * tokens / 1_000_000).quantize(Decimal("0.00000001"))
                assert call.input_tokens == tokens
            else:
                expected_usd = Decimal("0.00001234")
                assert call.input_tokens == 123 and call.cache_read_tokens == 11
            assert call.cost_usd == expected_usd and call.funding == "paid" and call.role == "rule_screen"
            assert held.state == "captured" and held.captured == config.credits_for_usd(expected_usd)
            assert 0 < held.captured < 1
        assert await balance(s, shared) == 2 - held.captured
        assert await balance(s, person) == 20


async def test_an_unfunded_chat_skips_all_calls_even_with_a_funded_sender(
    database: Database, settings: Settings
) -> None:
    chat, rule, shared, person = await group_rule(database)
    config = settings.model_copy(update={"decisions": Decisions()})
    async with database.transaction() as s:
        await credit(s, shared, "last-microcoin", Decimal("0.000001"), "grant")
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(
        base_url="https://provider.test/", transport=httpx.MockTransport(provider)
    ) as client:
        decider = Decider(database, OpenRouterAPI(config, client), await pins(database, config), config)
        assert await decider.match_rules(tg(8, "a dog"), chat=chat, candidates=[rule], bot_id=42) == set()
    assert not requests
    async with database.session() as s:
        assert (
            await s.scalar(select(ProviderCall.id)) is None and await s.scalar(select(Reservation.id)) is None
        )
        assert await balance(s, shared) == Decimal("0.000001") and await balance(s, person) == 20


async def test_concurrent_screens_respect_the_serving_openai_keys_committed_cap(
    database: Database, settings: Settings
) -> None:
    chat, rule, shared, person = await group_rule(database)
    config = settings.model_copy(
        update={"keys_secret": "synthetic-test-secret", "decisions": Decisions(timeout_seconds=5)}
    )
    anthropic = await add_key(database, config, 51, chats=(CHAT,))
    exhausted = await add_key(database, config, 52, chats=(CHAT,), cap="0.01")
    key_id = await add_key(database, config, 53, chats=(CHAT,), cap="0.01")
    async with database.transaction() as s:
        first = await s.get(ContributedKey, anthropic)
        assert first is not None
        first.provider, first.model = "anthropic", "anthropic/claude"
        s.add(
            ProviderCall(
                key_id=exhausted, model="test", kind="chat", funding="key", upstream_usd=Decimal("0.01")
            )
        )

    entered, finish = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        await finish.wait()
        return httpx.Response(
            200,
            json={
                "answers": {f"rule_{rule.id}": {"type": "noul", "noul": 0.96}},
                "usage": {
                    "input_tokens": 123,
                    "cost": 0,
                    "is_byok": True,
                    "cost_details": {"upstream_inference_cost": "0.00001234"},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/", transport=httpx.MockTransport(provider)
    ) as client:
        decider = Decider(database, OpenRouterAPI(config, client), await pins(database, config), config)
        running = asyncio.create_task(
            decider.match_rules(tg(8, "a dog"), chat=chat, candidates=[rule], bot_id=42)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            async with database.transaction() as s:
                pending = (await s.scalars(select(ProviderCall).where(ProviderCall.key_id == key_id))).one()
                assert pending.upstream_usd is not None and 0 < pending.upstream_usd < Decimal("0.005")
                # Leave room for just one check: its provisional cost must count while HTTP is pending.
                s.add(
                    ProviderCall(
                        key_id=key_id,
                        model="other",
                        kind="chat",
                        funding="key",
                        upstream_usd=Decimal("0.01") - pending.upstream_usd * Decimal("1.5"),
                    )
                )
            assert await decider.match_rules(tg(9, "a dog"), chat=chat, candidates=[rule], bot_id=42) == set()
        finally:
            finish.set()
            matched = await running
    assert matched == {rule.id}
    [request] = requests
    body = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer sk-or-sub-53"
    assert body["model"] == config.decisions.openai_model and body["provider"] == {
        "only": ["openai"],
        "allow_fallbacks": False,
    }
    async with database.session() as s:
        receipt = (await s.scalars(select(ProviderCall).where(ProviderCall.kind == "decision"))).one()
        assert receipt.key_id == key_id and receipt.funding == "key" and receipt.reservation_id is None
        assert receipt.cost_usd == 0 and receipt.upstream_usd == Decimal("0.00001234")
        assert await balance(s, shared) == 0 and await balance(s, person) == 20
        assert await s.scalar(select(Reservation.id)) is None


@pytest.mark.parametrize("personal", [False, True], ids=["chat_hold", "personal_hold"])
async def test_post_decision_on_a_rule_answer_is_paid_by_its_shared_hold_only(
    database: Database, settings: Settings, personal: bool
) -> None:
    _, _, shared, person = await group_rule(database)
    config = settings.model_copy(update={"decisions": Decisions()})
    payer = person if personal else shared
    async with database.transaction() as s:
        await credit(s, shared, "group-grant", 2, "grant")
        held = await reserve(s, payer, "automatic-run", 1)
        assert held is not None
        run = await start_run(
            s,
            chat_id=CHAT,
            user_id=1,
            trigger="rule",
            role="chat",
            funding="paid",
            wallet_id=payer,
            reservation_id=held.id,
        )
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "answers": {"effort": {"probabilities": {"2": 1}}},
                "usage": {"cost": "0.00001234", "input_tokens": 123},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/", transport=httpx.MockTransport(provider)
    ) as client:
        decider = Decider(database, OpenRouterAPI(config, client), await pins(database, config), config)
        verdict = await decider.decide(tg(8, "a dog"), bot_id=42, tz=UTC_TZ, group_run=run.id, effort=True)
    assert len(requests) == (0 if personal else 1)
    assert verdict.effort == (None if personal else "high")
    async with database.transaction() as s:
        [reservation] = list(await s.scalars(select(Reservation)))
        assert reservation.id == held.id and reservation.state == "held"
        costs = list(await s.scalars(select(ProviderCall)))
        if personal:
            assert not costs
        else:
            [call] = costs
            assert call.funding == "paid" and call.run_id == run.id and call.reservation_id is None
        charge = await Funder(database, config).settle(
            s, Funds(Funding.PAID, "automatic-run", payer, held.id, held.amount), run.id, delivered=True
        )
        assert charge == (Decimal(0) if personal else Decimal("0.014808"))
        assert await balance(s, person) == 20
        assert await balance(s, shared) == 2 - charge


async def test_a_key_only_automatic_run_uses_its_selected_openai_key_for_post_decisions(
    database: Database, settings: Settings
) -> None:
    _, _, shared, person = await group_rule(database)
    config = settings.model_copy(update={"keys_secret": "synthetic-test-secret", "decisions": Decisions()})
    key_id = await add_key(database, config, 56, chats=(CHAT,))
    funder = Funder(database, config)
    funds = await funder.reserve(
        chat_id=CHAT, private=False, user_id=1, operation_id="key-only", group_only=True
    )
    assert funds is not None and funds.key is not None and funds.key.key_id == key_id
    assert funds.reservation_id is None and funds.wallet_id is None
    async with database.transaction() as s:
        run = await start_run(
            s, chat_id=CHAT, user_id=1, trigger="rule", role="chat", funding="paid", key_id=key_id
        )

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-or-sub-56"
        return httpx.Response(
            200,
            json={
                "answers": {"effort": {"probabilities": {"2": 1}}},
                "usage": {"is_byok": True, "input_tokens": 135, "cost": 0},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/", transport=httpx.MockTransport(provider)
    ) as client:
        decider = Decider(database, OpenRouterAPI(config, client), await pins(database, config), config)
        verdict = await decider.decide(
            tg(8, "a dog"), bot_id=42, tz=UTC_TZ, key=funds.key, group_run=run.id, effort=True
        )
    assert verdict.effort == "high"
    async with database.transaction() as s:
        receipt = (await s.scalars(select(ProviderCall))).one()
        assert receipt.key_id == key_id and receipt.run_id == run.id and receipt.funding == "key"
        assert receipt.upstream_usd == Decimal("0.00001350") and receipt.cost_usd == 0
        assert await funder.settle(s, funds, run.id, delivered=True) == 0
        assert await s.scalar(select(Reservation.id)) is None
        assert await balance(s, shared) == 0 and await balance(s, person) == 20
