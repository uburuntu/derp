from sqlalchemy import update

from derp.history.models import Chat
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

IVAN = Person(1001, "Ivan", language_code="ru")
JANE = Person(1002, "Jane")
NEW = Room(-1011, title="Fresh")
OLD = Room(-1012, title="Veterans")


def added(room: Room, by: Person, bot_user: dict[str, object], status: str = "member") -> dict[str, object]:
    return {
        "chat": room.payload(),
        "from": by.payload(),
        "date": 0,
        "old_chat_member": {"status": "left", "user": bot_user},
        "new_chat_member": {"status": status, "user": bot_user},
    }


async def test_join_notice_in_the_inviters_language(conversation: Conversation) -> None:
    conversation.meet(IVAN, NEW, status="creator")
    before = conversation.seq
    update_id = await conversation.push("my_chat_member", added(NEW, IVAN, conversation.api.bot_user))
    await conversation.settle(update_id)
    (notice,) = [
        c for c in conversation.api.calls_of("sendRichMessage", after=before) if int(c["chat_id"]) == NEW.id
    ]
    text = notice["rich_message"]["markdown"]
    assert text.startswith("👋 **Дерп на связи**") and "@DerpRobot" in text
    assert "хранятся 30 дн." in text  # ambient is on and Derp reads all messages
    assert not notice.fields.get("reply_markup")  # so no make-me-admin link

    # Added again later (e.g. after being removed): no second notice.
    await conversation.settle(
        await conversation.push("my_chat_member", added(NEW, IVAN, conversation.api.bot_user))
    )
    assert (
        len(
            [
                c
                for c in conversation.api.calls_of("sendRichMessage", after=before)
                if int(c["chat_id"]) == NEW.id
            ]
        )
        == 1
    )


async def test_existing_group_gets_the_notice_on_first_invocation(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(JANE, OLD)
    chatter = await conversation.send(JANE, OLD, "hi all")
    await conversation.settle(chatter)
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == OLD.id).values(ambient=False))
    first = await conversation.send(JANE, OLD, "derp hello")
    reply = await conversation.expect_reply(first)
    notices = [
        c for c in conversation.api.calls_of("sendRichMessage", after=first.after)
        if int(c["chat_id"]) == OLD.id and c.sequence < reply.sequence
    ]  # fmt: skip
    assert len(notices) == 1
    assert "Mentions only" in notices[0]["rich_message"]["markdown"]
    await conversation.settle(first)
    second = await conversation.send(JANE, OLD, "derp again")
    await conversation.expect_reply(second)
    await conversation.settle(second)
    assert (
        len(
            [
                c
                for c in conversation.api.calls_of("sendRichMessage", after=second.after)
                if "👋" in str(c.fields)
            ]
        )
        == 0
    )
