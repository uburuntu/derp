from datetime import date

from sqlalchemy import func, select

from derp.agent.models import AgentRun
from derp.history.models import Chat, GameScore, Message
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
    assert len(prompt["reply_markup"]["inline_keyboard"]) == 1  # the whole chat is an admin's button
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


async def test_an_admin_forgets_the_whole_chat_and_the_chat_is_told(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(HAL, DEN, status="administrator")
    conversation.meet(IVY, DEN)
    model.reply("noted")
    first = await conversation.send(IVY, DEN, "derp the wifi password is hunter2")
    await conversation.expect_reply(first)
    await conversation.settle(first)
    async with conversation.db.transaction() as session:
        session.add(Note(chat_id=DEN.id, text="wifi is hunter2", author_id=IVY.id))
        session.add(Note(chat_id=DEN.id, user_id=IVY.id, text="likes tea", author_id=IVY.id))
        session.add(GameScore(chat_id=DEN.id, user_id=IVY.id, day=date(2026, 10, 1), points=3, answers=1))
        chat = await session.get(Chat, DEN.id)
        assert chat is not None
        chat.rules = "Be kind."

    asked = await conversation.send(HAL, DEN, "/forget")
    rows = (await conversation.expect_reply(asked))["reply_markup"]["inline_keyboard"]
    whole = rows[1][0]
    assert whole["text"] == "Forget the whole chat"
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == DEN.id)

    def last_edit(after: int) -> dict:
        return dict(conversation.api.calls_of("editMessageText", after=after)[-1].fields)

    after = conversation.seq
    await conversation.press(IVY, DEN.id, prompt_id, whole["callback_data"])  # not hers to press
    assert not conversation.api.calls_of("editMessageText", after=after)
    after = conversation.seq
    await conversation.press(HAL, DEN.id, prompt_id, whole["callback_data"])
    asking = last_edit(after)
    assert "All stored messages (**2**)" in asking["rich_message"]["markdown"]  # Ivy's and Derp's answer
    assert "**2** notes" in asking["rich_message"]["markdown"]
    go, back = asking["reply_markup"]["inline_keyboard"][0]

    after = conversation.seq
    await conversation.press(HAL, DEN.id, prompt_id, back["callback_data"])
    assert last_edit(after)["reply_markup"]["inline_keyboard"][1][0]["text"] == "Forget the whole chat"
    await conversation.press(HAL, DEN.id, prompt_id, whole["callback_data"])

    conversation.api.set_member_status(chat_id=DEN.id, user_id=HAL.id, status="member")
    after = conversation.seq
    await conversation.press(HAL, DEN.id, prompt_id, go["callback_data"])  # no longer an admin
    assert "Only admins" in str(conversation.api.calls_of("answerCallbackQuery", after=after)[-1]["text"])
    assert (await stored(conversation, first.message_id)).text is not None

    conversation.api.set_member_status(chat_id=DEN.id, user_id=HAL.id, status="administrator")
    after = conversation.seq
    await conversation.press(HAL, DEN.id, prompt_id, go["callback_data"], taps=2)
    notices = conversation.api.calls_of("editMessageText", after=after)
    assert len(notices) == 1  # a double tap announces once
    assert (
        "Hal wiped my memory of this chat: 2 messages and 2 notes" in notices[0]["rich_message"]["markdown"]
    )
    assert "reply_markup" not in conversation.api.message(DEN.id, prompt_id)
    async with conversation.db.session() as session:
        live = select(func.count()).where(Message.chat_id == DEN.id, Message.deleted_at.is_(None))
        assert await session.scalar(live) == 0
        assert await session.scalar(select(Note.id)) is None
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.messages is None
        chat = await session.get(Chat, DEN.id)
        assert chat is not None and chat.rules == "Be kind."  # rules, games, settings and money stay
        assert await session.scalar(select(GameScore.points)) == 3
