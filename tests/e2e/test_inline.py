from sqlalchemy import select

from derp.agent.models import AgentRun
from derp.history.models import Message
from derp.money.wallets import credit, wallet
from tests.e2e.harness import Conversation, Person
from tests.e2e.model import ScriptedModel

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
