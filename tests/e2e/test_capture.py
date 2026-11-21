from sqlalchemy import select, update

from derp.agent.models import AgentRun
from derp.history.models import Chat, Message
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel
from tests.e2e.test_chat import prompt_text

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


async def test_other_bot_messages_are_context_only(conversation: Conversation, model: ScriptedModel) -> None:
    peer = Person(202, "Scorekeeper", is_bot=True)
    conversation.meet(peer, GROUP)
    after = conversation.seq
    score = await conversation.send(
        peer, GROUP, "@DerpRobot final score 2–1", entities=[{"type": "mention", "offset": 0, "length": 10}]
    )
    await conversation.settle(score)
    edited = dict(score.message) | {"text": "@DerpRobot final score 3–1"}
    await conversation.settle(await conversation.push("edited_message", edited))
    for room in (GROUP, Room.private(peer)):
        await conversation.settle(await conversation.send(peer, room, "/help@DerpRobot"))
    await conversation.settle(
        await conversation.send(
            peer,
            GROUP,
            "/help@DerpRobot",
            sender_chat={
                "id": -100999,
                "type": "channel",
                "title": "A linked feed",
            },
            is_automatic_forward=True,
        )
    )
    await conversation.settle(await conversation.send(peer, text="derp, are you there?"))
    await conversation.settle(await conversation.push("guest_message", edited | {"guest_query_id": "peer-1"}))

    async with conversation.db.transaction() as session:
        stored = await session.get(Message, (GROUP.id, score.message_id))
        assert stored is not None and stored.capture == "ambient" and stored.text == edited["text"]
        await session.execute(update(Chat).where(Chat.id == GROUP.id).values(ambient=False))
    await conversation.settle(await conversation.send(peer, GROUP, "derp, new score"))
    assert await captures(conversation) == {score.message_id: "ambient"}
    async with conversation.db.session() as session:
        assert await session.scalar(select(AgentRun.id)) is None
        assert await session.scalar(select(Message.message_id).where(Message.chat_id == peer.id)) is None
    assert all(call.method.startswith("get") for call in conversation.api.calls[after:])

    asked = await conversation.send(ANN, GROUP, "derp, who won?", reply_to=score.message_id)
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "final score 3–1" in prompt_text(model.requests[-1])

    async with conversation.db.session() as session:
        answer_id = await session.scalar(select(Message.message_id).where(Message.is_derp))
    assert answer_id is not None
    after = conversation.seq
    await conversation.settle(await conversation.send(peer, GROUP, "one more?", reply_to=answer_id))
    assert all(call.method.startswith("get") for call in conversation.api.calls[after:])
