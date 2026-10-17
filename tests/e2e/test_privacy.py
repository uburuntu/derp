from sqlalchemy import select

from derp.agent.models import AgentRun
from derp.history.models import Message
from derp.memory.models import Note
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

HAL = Person(1401, "Hal")
IVY = Person(1402, "Ivy")
DEN = Room(-1014, title="Den")


async def stored(conversation: Conversation, message_id: int) -> Message:
    async with conversation.db.session() as session:
        row = await session.get(Message, (DEN.id, message_id))
    assert row is not None
    return row


async def test_forget_one_of_your_own_messages_but_not_someone_elses(conversation: Conversation) -> None:
    conversation.meet(HAL, DEN)
    conversation.meet(IVY, DEN)
    mine = await conversation.send(HAL, DEN, "my address is 1 Elm St")
    theirs = await conversation.send(IVY, DEN, "nice")
    await conversation.settle(theirs)

    refused = await conversation.send(HAL, DEN, "/forget", reply_to=theirs.message_id)
    assert "only forget your own" in (await conversation.expect_reply(refused))["rich_message"]["markdown"]
    assert (await stored(conversation, theirs.message_id)).text == "nice"

    asked = await conversation.send(HAL, DEN, "/forget", reply_to=mine.message_id)
    assert "forgotten" in (await conversation.expect_reply(asked))["rich_message"]["markdown"]
    row = await stored(conversation, mine.message_id)
    assert row.text is None and row.deleted_at is not None


async def test_forget_everything_after_confirming(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(HAL, DEN)
    conversation.meet(IVY, DEN)
    model.reply("noted")
    first = await conversation.send(HAL, DEN, "derp I'm allergic to cats")
    await conversation.expect_reply(first)
    await conversation.settle(first)
    async with conversation.db.transaction() as session:
        session.add(Note(chat_id=DEN.id, user_id=HAL.id, text="allergic to cats", author_id=HAL.id))
    other = await conversation.send(IVY, DEN, "same")
    await conversation.settle(other)

    asked = await conversation.send(HAL, DEN, "/forget")
    prompt = await conversation.expect_reply(asked)
    text = prompt["rich_message"]["markdown"]
    assert "Stored messages: **1**" in text  # commands are never stored
    data = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == DEN.id)

    after = conversation.seq
    await conversation.press(IVY, DEN.id, prompt_id, data)
    assert "for the person who asked" in str(
        conversation.api.calls_of("answerCallbackQuery", after=after)[-1]["text"]
    )
    assert (await stored(conversation, first.message_id)).text is not None

    after = conversation.seq
    await conversation.press(HAL, DEN.id, prompt_id, data)
    closed = conversation.api.calls_of("editMessageText", after=after)[-1]
    assert "your messages here (1)" in closed["rich_message"]["markdown"]  # the prompt says what went
    assert "reply_markup" not in conversation.api.message(DEN.id, prompt_id)
    assert (await stored(conversation, first.message_id)).text is None
    assert (await stored(conversation, other.message_id)).text == "same"
    async with conversation.db.session() as session:
        assert await session.scalar(select(Note.id)) is None
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.messages is None and run.status == "completed"
