from datetime import UTC, datetime
from typing import Any

from derp.agent.models import AgentRun
from derp.money.wallets import credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

ERIN = Person(1201, "Erin")
FINN = Person(1202, "Finn")
LOFT = Room(-1012, title="Loft")


async def ephemeral_command(conversation: Conversation, person: Person, room: Room, text: str) -> int:
    """An ephemeral command: message id 0, visible only to the sender and Derp."""
    command = text.split()[0]
    message: dict[str, Any] = {
        "message_id": 0,
        "ephemeral_message_id": 77,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": room.payload(person),
        "from": person.payload(),
        "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
    }
    return await conversation.push("message", message)


async def test_private_credits_show_bought_and_free_allowance(conversation: Conversation) -> None:
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", ERIN.id)).id, "stars:e", 650, "purchase")
    asked = await conversation.send(ERIN, text="/credits")
    text = (await conversation.expect_reply(asked))["rich_message"]["markdown"]
    assert "Bought: **650**" in text and "Free today: **10** of 10" in text and "This chat" not in text


async def test_group_credits_are_ephemeral_and_admins_see_chat_usage(conversation: Conversation) -> None:
    conversation.meet(ERIN, LOFT, status="administrator")
    conversation.meet(FINN, LOFT)
    hello = await conversation.send(FINN, LOFT, "hi")
    await conversation.settle(hello)
    async with conversation.db.transaction() as session:
        chat_wallet = (await wallet(session, "chat", LOFT.id)).id
        await credit(session, chat_wallet, "stars:loft", 3250, "purchase")
        session.add(
            AgentRun(
                chat_id=LOFT.id, user_id=FINN.id, trigger="message", role="chat", funding="paid",
                wallet_id=chat_wallet, charged_credits=12, status="completed",
            )
        )  # fmt: skip
    for person, admin in ((ERIN, True), (FINN, False)):
        after = conversation.seq
        await conversation.settle(await ephemeral_command(conversation, person, LOFT, "/credits"))
        reply = conversation.api.calls_of("sendRichMessage", after=after)[-1]
        assert int(reply["ephemeral_message_parameters"]["receiver_user_id"]) == person.id
        assert int(reply["reply_parameters"]["ephemeral_message_id"]) == 77
        text = reply["rich_message"]["markdown"]
        assert "This chat: **3,250**" in text
        assert ("Finn — 12" in text) is admin


async def test_info_shows_the_receipt_of_an_answer(conversation: Conversation, model: ScriptedModel) -> None:
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", ERIN.id)).id, "stars:e", 650, "purchase")
    model.cost = 0.01
    model.reply("forty-two")
    asked = await conversation.send(ERIN, text="meaning of life?")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    answer_id = max(mid for (cid, mid) in conversation.api.messages if cid == ERIN.id)
    info = await conversation.send(ERIN, text="/info", reply_to=answer_id)
    text = (await conversation.expect_reply(info))["rich_message"]["markdown"]
    assert "Charged: **12** Derpcoins — from personal Derpcoins" in text
    stray = await conversation.send(ERIN, text="/info")
    assert (
        "Reply to one of my messages" in (await conversation.expect_reply(stray))["rich_message"]["markdown"]
    )
