from tests.e2e.harness import Conversation, Person

GUS = Person(1301, "Gus", language_code="ru")
OWNER = Person(1000, "Owner")


async def test_terms_and_privacy_are_collapsible_pages(conversation: Conversation) -> None:
    for command, heading in (("/terms", "условия"), ("/privacy", "приватность")):
        asked = await conversation.send(GUS, text=command)
        text = (await conversation.expect_reply(asked))["rich_message"]["markdown"]
        assert heading in text and text.count("<details>") >= 3


async def test_paysupport_reaches_the_owner_in_the_users_words(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    empty = await conversation.send(GUS, text="/paysupport")
    assert "/paysupport" in (await conversation.expect_reply(empty))["rich_message"]["markdown"]
    asked = await conversation.send(GUS, text="/paysupport paid 50 stars yesterday, got nothing")
    assert "Stars" in (await conversation.expect_reply(asked))["rich_message"]["markdown"]
    pings = [c for c in conversation.api.calls_of("sendMessage") if int(c["chat_id"]) == OWNER.id]
    assert (
        len(pings) == 1
        and "paid 50 stars yesterday" in str(pings[0]["text"])
        and str(GUS.id) in str(pings[0]["text"])
    )
