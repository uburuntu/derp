from derp.agent.models import AgentRun
from derp.money.wallets import credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

ERIN = Person(1201, "Erin")
FINN = Person(1202, "Finn")
LOFT = Room(-1012, title="Loft")


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
        await conversation.settle(await conversation.ephemeral_command(person, LOFT, "/credits"))
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


async def test_plain_group_credits_go_by_dm_never_into_the_group(conversation: Conversation) -> None:
    conversation.meet(ERIN)
    conversation.meet(ERIN, LOFT, status="administrator")
    conversation.meet(FINN, LOFT)
    after = conversation.seq
    asked = await conversation.send(ERIN, LOFT, "/credits")
    await conversation.settle(asked)
    sent = conversation.api.calls_of("sendRichMessage", after=after)
    private = [c for c in sent if int(c["chat_id"]) == ERIN.id]
    public = [c["rich_message"]["markdown"] for c in sent if int(c["chat_id"]) == LOFT.id]
    assert private and "Free today" in private[0]["rich_message"]["markdown"]
    assert public == ["Sent you the details in our private chat."]

    conversation.api.fail_next(
        "sendRichMessage",
        error_code=403,
        description="Forbidden: bot can't initiate conversation with a user",
    )
    stranger = await conversation.send(FINN, LOFT, "/credits")
    reply = (await conversation.expect_reply(stranger))["rich_message"]["markdown"]
    assert "Open a private chat with me" in reply
