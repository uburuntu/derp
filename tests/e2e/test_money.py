from decimal import Decimal

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import func, select

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.inference.models import ProviderCall
from derp.money.models import Reservation
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person
from tests.e2e.model import ScriptedModel, tool_call

CARL = Person(601, "Carl")
OWNER = Person(1000, "Owner")


async def buy(conversation: Conversation, person: Person, amount: int) -> int:
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", person.id)).id
        await credit(session, wallet_id, f"stars:{person.id}", amount, "purchase")
    return wallet_id


async def test_paid_answer_is_charged_its_marked_up_cost(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 500)
    model.cost = 0.01  # ceil(0.01 × 1.2 markup / $0.001) = 12 credits
    model.reply("sure")
    sent = await conversation.send(CARL, text="hi derp")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert model.pins[-1].model != "deepseek/deepseek-v4.1-flash"  # paid runs use the standard chat model
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.funding, run.charged_credits, run.wallet_id) == (
            "paid",
            12,
            wallet_id,
        )
        assert await balance(session, wallet_id) == 488


async def test_failed_run_releases_the_hold(conversation: Conversation, model: ScriptedModel) -> None:
    wallet_id = await buy(conversation, CARL, 500)

    def boom(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider exploded")

    model.reply(boom)
    sent = await conversation.send(CARL, text="hi derp")
    reply = await conversation.expect_reply(sent)
    assert "weren't charged" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        assert await balance(session, wallet_id) == 500
        assert await session.scalar(select(Reservation.state)) == "released"


async def test_run_stops_at_the_credits_it_could_hold(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 5)
    model.cost = 0.004  # 5 credits per model turn: the first one uses up the hold
    model.reply(tool_call("search_history", query="cats"), "never reached")
    sent = await conversation.send(CARL, text="find cats")
    reply = await conversation.expect_reply(sent)
    assert "too expensive" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    assert len(model.requests) == 1
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.charged_credits) == ("cancelled", 0)
        assert await balance(session, wallet_id) == 5  # nothing was delivered, so nothing is charged


async def test_no_allowance_left_says_so_without_running(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        session.add(
            ProviderCall(
                kind="chat",
                model="m",
                funding="sponsored",
                cost_usd=Decimal(str(settings.sponsored_global_daily_usd)),
            )
        )
    sent = await conversation.send(CARL, text="hi derp")
    reply = await conversation.expect_reply(sent)
    assert "free Derpcoins are used up" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    assert model.requests == []
    async with conversation.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0
    pings = [c for c in conversation.api.calls if c.method == "sendMessage" and int(c["chat_id"]) == OWNER.id]
    assert len(pings) == 1 and "cap" in str(pings[0]["text"])
