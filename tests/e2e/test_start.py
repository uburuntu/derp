from tests.e2e.harness import Conversation, Person


async def test_private_start_replies_in_users_language(conversation: Conversation) -> None:
    anna = Person(id=101, first_name="Anna", language_code="ru")
    sent = await conversation.send(anna, text="/start")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"].startswith("**Дерп**")
    assert reply["reply_parameters"]["message_id"] == sent.message_id
