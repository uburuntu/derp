from sqlalchemy import select, update

from derp.history.models import Chat, Message
from tests.e2e.harness import Conversation, Person, Room

GROUP = Room(-1001, title="Friends")
ANN = Person(201, "Ann")


async def captures(conversation: Conversation) -> dict[int, str]:
    async with conversation.db.session() as session:
        rows = await session.execute(
            select(Message.message_id, Message.capture).where(Message.chat_id == GROUP.id)
        )
        return {row[0]: row[1] for row in rows}


async def test_group_messages_are_captured_by_policy(conversation: Conversation) -> None:
    conversation.meet(ANN, GROUP)
    ambient = await conversation.send(ANN, GROUP, "just chatting")
    await conversation.settle(ambient)
    assert await captures(conversation) == {ambient.message_id: "ambient"}

    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == GROUP.id).values(ambient=False))
    quiet = await conversation.send(ANN, GROUP, "not stored")
    invoked = await conversation.send(ANN, GROUP, "дерп, привет")
    await conversation.settle(invoked)
    stored = await captures(conversation)
    assert quiet.message_id not in stored
    assert stored[invoked.message_id] == "invocation"

    # With context off, an edit updates a message already kept and still skips the rest.
    for sent, text in ((invoked, "дерп, привет всем"), (quiet, "still not stored")):
        edited = dict(sent.message) | {"edit_date": int(sent.message["date"]) + 60, "text": text}
        await conversation.settle(await conversation.push("edited_message", edited))
    async with conversation.db.session() as session:
        rows = await session.execute(
            select(Message.message_id, Message.text).where(Message.chat_id == GROUP.id)
        )
        texts = {row[0]: row[1] for row in rows}
    assert texts[invoked.message_id] == "дерп, привет всем" and quiet.message_id not in texts
