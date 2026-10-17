from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import insert, select, update

from derp.history.models import Chat, Message, MessageMedia, Sticker
from derp.inference.models import ProviderCall
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DORA = Person(601, "Dora")


def photo(file_id: str) -> list[dict[str, object]]:
    return [
        {"file_id": f"{file_id}-s", "file_unique_id": f"u-{file_id}-s", "width": 90, "height": 90},
        {
            "file_id": file_id,
            "file_unique_id": f"u-{file_id}",
            "width": 800,
            "height": 600,
            "file_size": len(PNG),
        },
    ]


def images_in(messages: list[ModelMessage]) -> list[BinaryContent]:
    found: list[BinaryContent] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    found.extend(c for c in content if isinstance(c, BinaryContent))
    return found


async def test_photo_question_sends_pixels_and_stores_a_note(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.api.register_file("cat-photo", PNG)

    def look(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (image,) = images_in(messages[-1:])
        assert image.media_type == "image/png" and image.data == PNG
        return ModelResponse(parts=[TextPart("a very square cat")])

    model.reply(look, "grey square cat on black background")
    sent = await conversation.send(DORA, caption="what is this?", photo=photo("cat-photo"))
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "a very square cat"
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == sent.message_id))
        assert media is not None and media.description == "grey square cat on black background"
        row = await session.get(Message, (DORA.id, sent.message_id))
        assert row is not None and row.extra_text == media.description
        kinds = list(await session.scalars(select(ProviderCall.kind)))
        assert sorted(kinds) == ["chat", "media"]

    # The next turn sees the note in the window instead of re-downloading.
    model.reply("still a cat")
    later = await conversation.send(DORA, text="and now?")
    await conversation.expect_reply(later)
    window_text = str(
        [p.content for m in model.requests[-1] for p in m.parts if isinstance(p, UserPromptPart)]
    )
    assert "[photo: grey square cat on black background]" in window_text


async def test_inspect_media_tool_opens_an_older_image(
    conversation: Conversation, model: ScriptedModel
) -> None:
    group = Room(-1007, title="Pics")
    conversation.meet(DORA, group)
    conversation.api.register_file("meme", PNG)
    posted = await conversation.send(DORA, group, photo=photo("meme"))
    await conversation.settle(posted)
    assert model.requests == []  # ambient photos are not downloaded

    def call(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("inspect_media", {"message_id": posted.message_id}, tool_call_id="m1")]
        )

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert images_in(messages), "the tool returns the pixels to a vision model"
        return ModelResponse(parts=[TextPart("it's a meme")])

    model.reply(call, "a meme with a frog", answer)
    asked = await conversation.send(DORA, group, f"дерп, что на #{posted.message_id}?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "it's a meme"
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == posted.message_id))
        assert media is not None and media.description == "a meme with a frog"


async def test_replying_to_a_photo_derp_never_kept_still_shows_it(
    conversation: Conversation, model: ScriptedModel
) -> None:
    quiet = Room(-1008, title="Quiet")
    eve = Person(602, "Eve")
    conversation.meet(DORA, quiet)
    conversation.meet(eve, quiet)
    hello = await conversation.send(DORA, quiet, "hi all")
    await conversation.settle(hello)
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == quiet.id).values(ambient=False))
    conversation.api.register_file("cat", PNG)
    posted = await conversation.send(DORA, quiet, photo=photo("cat"))
    await conversation.settle(posted)
    async with conversation.db.session() as session:
        assert await session.get(Message, (quiet.id, posted.message_id)) is None  # ambient off: not kept

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert images_in(messages), "the replied-to photo reaches the model"
        return ModelResponse(parts=[TextPart("a cat, obviously")])

    model.reply(answer, "a grey square")  # the answer, then the image note
    asked = await conversation.send(eve, quiet, "derp what is this?", reply_to=posted.message_id)
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "a cat, obviously"


async def test_an_empty_image_note_is_paid_once_and_never_retried(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.api.register_file("blur", PNG)

    def thinking_only(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        # A reasoning model that spent its whole note budget thinking: billed, but no text.
        return ModelResponse(parts=[TextPart("")], finish_reason="length", provider_details={"cost": 0.0003})

    model.reply("blurry, can't tell", thinking_only)
    sent = await conversation.send(DORA, caption="what is this?", photo=photo("blur"))
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == sent.message_id))
        assert media is not None and media.description == ""  # tried: never described (or paid) again
        notes = list(await session.scalars(select(ProviderCall.cost_usd).where(ProviderCall.kind == "media")))
        assert [float(cost) for cost in notes] == [0.0003]

    model.reply("still blurry")
    asked = len(model.requests)
    again = await conversation.send(DORA, text="look again", reply_to=sent.message_id)
    await conversation.expect_reply(again)
    await conversation.settle(again)
    assert len(model.requests) == asked + 1  # the answer only, no second note
    async with conversation.db.session() as session:
        notes = list(await session.scalars(select(ProviderCall.id).where(ProviderCall.kind == "media")))
        assert len(notes) == 1


async def test_media_and_stickers_of_another_bot_degrade_instead_of_failing(
    conversation: Conversation, model: ScriptedModel
) -> None:
    yard = Room(-1019, title="Old yard")
    conversation.meet(DORA, yard)
    conversation.api.register_file("old-photo", PNG)
    shared = await conversation.send(DORA, yard, photo=photo("old-photo"))
    await conversation.settle(shared)
    async with conversation.db.transaction() as session:  # as if captured under a previous bot
        await session.execute(update(MessageMedia).values(bot_id=1))
        await session.execute(
            insert(Sticker).values(bot_id=1, file_unique_id="u-st", file_id="old", emoji="😂")
        )

    def look(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert images_in(messages) == []  # never requested from Telegram, so no pixels
        return ModelResponse(parts=[TextPart("can't see it anymore")])

    model.reply(look, tool_call("send_sticker", emoji="😂"), "fine")
    asked = await conversation.send(DORA, yard, "derp what is this?", reply_to=shared.message_id)
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "can't see it anymore"
    assert not conversation.api.calls_of("getFile", after=shared.after)
    await conversation.settle(asked)
    again = await conversation.send(DORA, yard, "derp send a 😂 sticker")
    await conversation.settle(again)
    assert not conversation.api.calls_of("sendSticker", after=again.after)
    assert "I don't know a sticker" in str(model.requests[-1][-1])
