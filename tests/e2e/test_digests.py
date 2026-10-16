from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from derp.history.models import Chat, Digest, Message
from derp.inference.models import ProviderCall
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

GIA = Person(1301, "Gia")
PORCH = Room(-1013, title="Porch")


async def seed_yesterday(conversation: Conversation) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with conversation.db.transaction() as session:
        session.add(Chat(id=PORCH.id, type="supergroup", title="Porch"))
        await session.flush()
        session.add_all(
            Message(
                chat_id=PORCH.id, message_id=conversation.api.take_message_id(PORCH.id), user_id=GIA.id,
                date=yesterday.replace(hour=12, minute=n), kind="text", text=text, capture="ambient", payload={},
            )
            for n, text in enumerate(["picnic on saturday?", "I'll bring lemonade"])
        )  # fmt: skip


async def test_yesterday_is_summarized_once_then_served_from_cache(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(GIA, PORCH)
    await seed_yesterday(conversation)
    model.reply(tool_call("digest", period="yesterday"), "- Gia planned a Saturday picnic (#1)", "a picnic!")
    asked = await conversation.send(GIA, PORCH, "derp what happened yesterday?")
    assert (await conversation.expect_reply(asked))["rich_message"]["markdown"] == "a picnic!"
    await conversation.settle(asked)
    digest_request = model.requests[1]
    assert "picnic on saturday?" in str(digest_request) and "chat_history" in str(digest_request)
    async with conversation.db.session() as session:
        digest = await session.scalar(select(Digest))
        calls = list(await session.scalars(select(ProviderCall.kind).where(ProviderCall.run_id.is_not(None))))
    assert digest is not None and digest.summary.startswith("- Gia planned") and digest.message_count == 2
    assert "digest" in calls

    model.reply(tool_call("digest", period="yesterday"), "still a picnic")
    again = await conversation.send(GIA, PORCH, "derp and yesterday again?")
    await conversation.expect_reply(again)
    assert len(model.requests) == 5  # no new summarizing call
    assert "Gia planned a Saturday picnic" in str(model.requests[-1])


async def test_editing_a_summarized_message_marks_the_day_stale(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(GIA, PORCH)
    await seed_yesterday(conversation)
    model.reply(tool_call("digest", period="yesterday"), "- lemonade", "done")
    asked = await conversation.send(GIA, PORCH, "derp recap yesterday")
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        digest = await session.scalar(select(Digest))
        assert digest is not None and not digest.stale
        first = digest.first_message_id
    edited = {
        "message_id": first,
        "date": int((datetime.now(UTC) - timedelta(days=1)).timestamp()),
        "edit_date": int(datetime.now(UTC).timestamp()),
        "chat": PORCH.payload(GIA),
        "from": GIA.payload(),
        "text": "picnic on sunday?",
    }
    await conversation.settle(await conversation.push("edited_message", edited))
    async with conversation.db.session() as session:
        assert (await session.scalar(select(Digest.stale))) is True
