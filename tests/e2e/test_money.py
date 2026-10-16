from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import func, select

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.history.models import Chat, User
from derp.inference.models import ProviderCall
from derp.money.free import POLICY_VERSION
from derp.money.models import Reservation
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person, Room
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
    assert model.pins[0].model != "deepseek/deepseek-v4.1-flash"  # paid runs use the standard chat model
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


async def test_a_run_that_dies_outside_the_model_is_closed_and_released(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 500)

    def broken(_pin: object) -> object:
        raise RuntimeError("factory down")

    conversation.overrides["model_factory"] = broken
    await conversation.restart()
    sent = await conversation.send(CARL, text="hi derp")
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.error_type) == ("failed", "RuntimeError")
        assert await session.scalar(select(Reservation.state)) == "released"
        assert await balance(session, wallet_id) == 500


async def exhaust_sponsorship(conversation: Conversation, settings: Settings) -> None:
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        cap = Decimal(str(settings.sponsored_global_daily_usd))
        session.add(ProviderCall(kind="chat", model="m", funding="sponsored", cost_usd=cap))


async def press_button(conversation: Conversation, person: Person, chat_id: int, call: Any) -> None:
    data = call["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    message_id = max(mid for (cid, mid) in conversation.api.messages if cid == chat_id)
    await conversation.press(person, chat_id, message_id, data)


async def test_consenting_to_free_models_answers_the_waiting_question(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    asked = await conversation.send(CARL, text="what's 2+2?")
    offer = await conversation.expect_reply(asked)
    assert "may store" in offer["rich_message"]["markdown"]
    model.reply("4, on the house")
    after = conversation.seq
    await press_button(conversation, CARL, CARL.id, offer)
    answers = [
        c
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if "on the house" in c["rich_message"]["markdown"]
    ]
    assert answers and int(answers[0]["reply_parameters"]["message_id"]) == asked.message_id
    assert model.pins[-1].model.endswith(":free")
    async with conversation.db.session() as session:
        user = await session.get(User, CARL.id)
        assert user is not None and user.free_models_consent_version is not None
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.funding, run.charged_credits) == ("free", 0)


async def test_only_an_admin_turns_free_models_on_for_a_group(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    den = Room(-1013, title="Den")
    boss = Person(602, "Boss")
    conversation.meet(CARL, den)
    conversation.meet(boss, den, status="administrator")
    asked = await conversation.send(CARL, den, "derp hi")
    offer = await conversation.expect_reply(asked)
    await press_button(conversation, CARL, den.id, offer)  # not an admin
    async with conversation.db.session() as session:
        chat = await session.get(Chat, den.id)
        assert chat is not None and not chat.free_models
    model.reply("free hello")
    after = conversation.seq
    await press_button(conversation, boss, den.id, offer)
    texts = [c["rich_message"]["markdown"] for c in conversation.api.calls_of("sendRichMessage", after=after)]
    assert any("allowed free models" in text for text in texts) and "free hello" in texts
    async with conversation.db.session() as session:
        chat = await session.get(Chat, den.id)
        assert chat is not None and chat.free_models


async def test_busy_free_models_say_so(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    async with conversation.db.transaction() as session:
        session.add(User(id=CARL.id, first_name="Carl", free_models_consent_version=POLICY_VERSION))

    def busy(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=429, model_name="free")

    model.reply(busy)
    asked = await conversation.send(CARL, text="hi")
    assert "free models are busy" in (await conversation.expect_reply(asked))["rich_message"]["markdown"]


async def test_a_failure_before_the_model_runs_returns_the_hold(
    conversation: Conversation, model: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from derp.agent import runtime

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("window")

    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", CARL.id)).id, "stars:c", 650, "purchase")
    monkeypatch.setattr(runtime, "build_window", broken)
    sent = await conversation.send(CARL, text="hi derp")
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        assert await session.scalar(select(Reservation.state)) == "released"
        assert await session.scalar(select(AgentRun.id)) is None  # its row rolled back with the window
