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
from sqlalchemy import select

from derp.history.models import Message, MessageMedia
from derp.inference.models import ProviderCall
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

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
