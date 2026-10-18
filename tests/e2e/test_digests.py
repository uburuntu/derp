from datetime import UTC, datetime, timedelta

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select, update

from derp.history.models import Chat, Digest, Message
from derp.inference.models import ProviderCall
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

GIA = Person(1301, "Gia", username="gia_x")
HUGO = Person(1302, "Hugo")
PORCH = Room(-1013, title="Porch")
DEN = Room(-1001300000013, title="Den")


def tool_result(messages: list[ModelMessage], tool: str) -> str:
    return next(
        p.model_response_str()
        for m in messages
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == tool
    )


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


async def test_what_did_i_miss_since_my_last_message(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(GIA, DEN)
    conversation.meet(HUGO, DEN)
    left = await conversation.send(GIA, DEN, "heading out, back tonight")
    await conversation.settle(left)
    chatter = [
        await conversation.send(HUGO, DEN, "who's bringing the speaker on friday?"),
        await conversation.send(HUGO, DEN, "can you grab ice on the way back?", reply_to=left.message_id),
        await conversation.send(HUGO, DEN, "the band cancelled btw"),
    ]
    for sent in chatter:
        await conversation.settle(sent)
    ice = chatter[1].message_id

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        result = tool_result(messages, "digest")
        aimed, _, rest = result.partition(f"## After #{left.message_id}")
        assert aimed.startswith("## For the asker") and "can you grab ice" in aimed
        assert "3 messages" in rest and "the band cancelled btw" in rest  # short: shown as is
        return ModelResponse(parts=[TextPart(f"Hugo wants ice (#{ice}); the band cancelled")])

    model.reply(tool_call("digest", period="since_my_last_message"), answer)
    asked = await conversation.send(GIA, DEN, "derp what did I miss?")
    reply = (await conversation.expect_reply(asked))["rich_message"]["markdown"]
    assert reply == f"Hugo wants ice ([#{ice}](https://t.me/c/1300000013/{ice})); the band cancelled"
    await conversation.settle(asked)
    assert len(model.requests) == 2  # nothing needed summarizing
    async with conversation.db.session() as session:
        assert await session.scalar(select(Digest)) is None  # a stretch since a message is never cached


async def test_catching_up_from_a_replied_message_reads_its_day_then_digests_today(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(GIA, DEN)
    conversation.meet(HUGO, DEN)
    said = [
        await conversation.send(GIA, DEN, "picnic on saturday?"),
        await conversation.send(HUGO, DEN, "@gia_x will you bring lemonade?"),
        await conversation.send(HUGO, DEN, "rain on saturday, picnic moves to sunday"),
    ]
    for sent in said:
        await conversation.settle(sent)
    ids = [sent.message_id for sent in said]
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with conversation.db.transaction() as session:  # the first two were yesterday
        await session.execute(
            update(Message).where(Message.message_id == ids[0]).values(date=yesterday.replace(hour=12))
        )
        await session.execute(
            update(Message).where(Message.message_id == ids[1]).values(date=yesterday.replace(hour=13))
        )

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        result = tool_result(messages, "digest")
        aimed, rest, today = result.split("\n\n## ")
        assert aimed.startswith("## For the asker") and "@gia_x will you bring lemonade?" in aimed
        assert rest.startswith(f"After #{ids[0]}") and "1 messages" in rest  # the rest of that day, as is
        assert "picnic moves to sunday (#3)" in today  # then today's digest
        return ModelResponse(parts=[TextPart("picnic is sunday now")])

    model.reply(tool_call("digest", period="since_message"), "- picnic moves to sunday (#3)", answer)
    asked = await conversation.send(GIA, DEN, "derp catch me up from here", reply_to=ids[0])
    assert (await conversation.expect_reply(asked))["rich_message"]["markdown"] == "picnic is sunday now"
    await conversation.settle(asked)
    assert "rain on saturday" in str(model.requests[1]) and "lemonade" not in str(model.requests[1])
    async with conversation.db.session() as session:
        digest = await session.scalar(select(Digest))
    assert digest is not None and digest.day == datetime.now(UTC).date()  # only the whole day is cached
