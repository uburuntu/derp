from decimal import Decimal
from typing import Any

from pydantic_ai.messages import ToolReturnPart
from sqlalchemy import select

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.history.models import Message, User
from derp.inference.models import ModelPin
from derp.inference.roles import DEFAULT_PINS, ModelRole
from derp.money.free import POLICY_VERSION
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_money import exhaust_sponsorship

NIA = Person(1701, "Nia")


async def test_inline_answer_is_edited_into_the_sent_message(
    conversation: Conversation, model: ScriptedModel
) -> None:
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", NIA.id)).id, "stars:nia", 650, "purchase")
    after = conversation.seq
    query = await conversation.push(
        "inline_query", {"id": "iq1", "from": NIA.payload(), "query": "capital of peru?", "offset": ""}
    )
    await conversation.settle(query)
    answered = conversation.api.calls_of("answerInlineQuery", after=after)[-1]
    result = answered["results"][0]
    assert answered["is_personal"] in (True, "true") and int(answered["cache_time"]) == 0
    assert "capital of peru?" in result["input_message_content"]["message_text"]
    assert model.requests == []  # typing costs nothing; only a sent result runs the model

    inline_id = conversation.api.send_inline_result(str(result["id"]))
    model.reply("Lima 🦙")
    chosen = await conversation.push(
        "chosen_inline_result",
        {
            "result_id": result["id"],
            "from": NIA.payload(),
            "query": "capital of peru?",
            "inline_message_id": inline_id,
        },
    )
    await conversation.settle(chosen)
    edit = conversation.api.inline_messages[inline_id]
    assert edit["rich_message"]["markdown"] == "❓ capital of peru?\n\nLima 🦙"
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert (
            run is not None and run.trigger == "inline" and run.messages is None and run.status == "completed"
        )
    assert len(model.requests[-1]) == 1  # no chat history: just the question


async def test_guest_mention_gets_one_reply_and_nothing_is_stored(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(NIA)
    model.reply("hi from the outside")
    guest = {
        "message_id": 55,
        "date": 1792000000,
        "chat": {"id": -1999, "type": "supergroup", "title": "Elsewhere"},
        "from": NIA.payload(),
        "text": "@DerpRobot say hi",
        "guest_query_id": "gq-1",
    }
    update = await conversation.push("guest_message", guest)
    await conversation.settle(update)
    call = conversation.api.calls_of("answerGuestQuery")[-1]
    assert call["guest_query_id"] == "gq-1"
    assert call["result"]["input_message_content"]["rich_message"]["markdown"] == "hi from the outside"
    async with conversation.db.session() as session:
        assert await session.scalar(select(Message.message_id).where(Message.chat_id == -1999)) is None
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.trigger == "guest" and run.messages is None


async def ask_inline(conversation: Conversation, person: Person, query: str, query_id: str) -> str:
    """Type an inline query, send its result and wait for the answer; the inline message id."""
    after = conversation.seq
    typed = await conversation.push(
        "inline_query", {"id": query_id, "from": person.payload(), "query": query, "offset": ""}
    )
    await conversation.settle(typed)
    result = conversation.api.calls_of("answerInlineQuery", after=after)[-1]["results"][0]
    inline_id = conversation.api.send_inline_result(str(result["id"]))
    chosen = {
        "result_id": result["id"],
        "from": person.payload(),
        "query": query,
        "inline_message_id": inline_id,
    }
    await conversation.settle(await conversation.push("chosen_inline_result", chosen))
    return inline_id


CHAT_TOOLS = {
    "search_history",
    "read_messages",
    "send_poll",
    "react",
    "schedule",
    "generate_image",
    "wallet_status",
}


async def test_an_inline_answer_can_use_lookups_and_is_charged(
    conversation: Conversation, model: ScriptedModel, web: dict[str, Any]
) -> None:
    web["/v6/latest/USD"] = {"result": "success", "rates": {"RUB": 81.5}}
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", NIA.id)).id
        await credit(session, wallet_id, "stars:nia", 650, "purchase")
        prices = {"price_prompt": Decimal(1), "price_completion": Decimal(4)}  # USD per 1M tokens
        session.add(ModelPin(role="chat", pin=DEFAULT_PINS[ModelRole.CHAT].model_dump(mode="json"), **prices))
    model.reply(tool_call("exchange_rate", base="USD", quote="RUB", amount=100), "≈8150 ₽")
    inline_id = await ask_inline(conversation, NIA, "100 usd in rub", "iq-rate")
    assert conversation.api.inline_messages[inline_id]["rich_message"]["markdown"].endswith("≈8150 ₽")
    returned = [str(p.content) for m in model.requests[-1] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert returned == ["100 USD = 8,150 RUB (open.er-api.com, daily)"]
    names = {tool.name for tool in model.infos[-1].function_tools}
    assert {"exchange_rate", "weather", "about_derp", "read_source", "run_code"} <= names
    assert not names & CHAT_TOOLS and "profile_photo" not in names
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert (
            run is not None and run.status == "completed" and run.funding == "paid" and run.messages is None
        )
        assert run.charged_credits > 0 and await balance(session, wallet_id) == 650 - run.charged_credits


async def test_a_free_inline_answer_gets_only_the_free_tools(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    async with conversation.db.transaction() as session:
        session.add(User(id=NIA.id, first_name="Nia", free_models_consent_version=POLICY_VERSION))
    model.reply("no idea, free tier")
    await ask_inline(conversation, NIA, "what's new", "iq-free")
    names = {tool.name for tool in model.infos[-1].function_tools}
    assert "about_derp" in names and "search_tools" in names and "run_code" in names
    assert "read_source" not in names and not names & CHAT_TOOLS  # source reading is paid and sponsored only
    assert not model.infos[-1].model_request_parameters.native_tools  # no web on free models
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.funding == "free" and run.charged_credits == 0
