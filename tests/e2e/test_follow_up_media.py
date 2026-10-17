"""A photo, then the question: the asker's photo just before, or the one behind an answered run, is shown."""

from datetime import UTC, datetime

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select, update

from derp.agent.models import RunOutput
from derp.history.models import Message, MessageMedia
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import Responder, ScriptedModel
from tests.e2e.test_media import PNG, images_in, photo

DORA = Person(611, "Dora")
EVE = Person(612, "Eve")
YARD = Room(-1061, title="Yard")


def sees(answer: str, count: int = 1) -> Responder:
    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert len(images_in(messages[-1:])) == count
        return ModelResponse(parts=[TextPart(answer)])

    return respond


async def description(conversation: Conversation, message_id: int) -> str | None:
    async with conversation.db.session() as session:
        return await session.scalar(
            select(MessageMedia.description).where(
                MessageMedia.chat_id == YARD.id, MessageMedia.message_id == message_id
            )
        )


async def test_a_photo_then_the_question_shows_the_photo(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(DORA, YARD)
    conversation.meet(EVE, YARD)
    conversation.api.register_file("street", PNG)
    posted = await conversation.send(DORA, YARD, photo=photo("street"))
    await conversation.settle(posted)
    chatter = await conversation.send(EVE, YARD, "nice")  # someone else in between doesn't matter
    await conversation.settle(chatter)

    model.reply(sees("a street at night"), "a dark street with lamps")  # the answer, then the photo's note
    asked = await conversation.send(DORA, YARD, "дерп, что тут?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "a street at night"
    await conversation.settle(asked)
    assert f"(image from #{posted.message_id})" in str(model.requests[-2][-1])  # then the note's request
    assert await description(conversation, posted.message_id) == "a dark street with lamps"


async def test_an_older_photo_or_one_followed_by_more_is_not_shown(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(DORA, YARD)
    conversation.api.register_file("old", PNG)
    conversation.api.register_file("busy", PNG)
    conversation.api.register_file("gone", PNG)
    stale = await conversation.send(
        DORA, YARD, photo=photo("old"), date=int(datetime.now(UTC).timestamp()) - 120
    )
    await conversation.settle(stale)
    model.reply(sees("no picture here", count=0))
    asked = await conversation.send(DORA, YARD, "derp what's this?")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)

    busy = await conversation.send(DORA, YARD, photo=photo("busy"))
    await conversation.settle(busy)
    more = await conversation.send(DORA, YARD, "lol")  # the asker's own message in between
    await conversation.settle(more)
    model.reply(sees("still nothing", count=0))
    again = await conversation.send(DORA, YARD, "derp and this?")
    await conversation.expect_reply(again)
    await conversation.settle(again)

    gone = await conversation.send(DORA, YARD, photo=photo("gone"))
    await conversation.settle(gone)
    async with conversation.db.transaction() as session:  # forgotten: never shown
        await session.execute(
            update(Message)
            .where(Message.chat_id == YARD.id, Message.message_id == gone.message_id)
            .values(deleted_at=datetime.now(UTC))
        )
    model.reply(sees("nothing at all", count=0))
    last = await conversation.send(DORA, YARD, "derp what about now?")
    await conversation.expect_reply(last)
    await conversation.settle(last)
    assert await description(conversation, stale.message_id) is None
    assert await description(conversation, busy.message_id) is None


async def test_a_reply_to_derps_answer_about_a_photo_shows_it_again(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(DORA, YARD)
    conversation.meet(EVE, YARD)
    conversation.api.register_file("party", PNG)
    posted = await conversation.send(DORA, YARD, photo=photo("party"))
    await conversation.settle(posted)
    model.reply(sees("three people at a party"), "three people dancing")
    asked = await conversation.send(DORA, YARD, "derp who's here?")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    async with conversation.db.transaction() as session:  # as if the answering model had been blind
        answer = await session.scalar(select(RunOutput.message_id))
        await session.execute(
            update(MessageMedia).where(MessageMedia.message_id == posted.message_id).values(description=None)
        )

    model.reply(sees("the one on the left is holding a cake"))
    follow = await conversation.send(EVE, YARD, "а слева кто?", reply_to=answer)
    reply = await conversation.expect_reply(follow)
    assert reply["rich_message"]["markdown"] == "the one on the left is holding a cake"
    await conversation.settle(follow)
    # One hop back is shown but not described: its line is older than the previous question.
    assert await description(conversation, posted.message_id) is None
