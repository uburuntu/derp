import io
from datetime import UTC, datetime
from decimal import Decimal

from PIL import Image
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import insert, select, update

from derp.agent.watch import NO_QUESTION
from derp.history.models import Chat, Message, MessageMedia, Sticker
from derp.inference.models import ModelPin, ProviderCall
from derp.inference.roles import DEFAULT_PINS, ModelRole
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call


def png(color: str = "grey", size: tuple[int, int] = (32, 32)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = png()
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
        assert image.media_type == "image/jpeg"  # normalized for the model
        assert Image.open(io.BytesIO(image.data)).size == (32, 32)
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
    earlier = int(datetime.now(UTC).timestamp()) - 600  # too old to come along with the question by itself
    posted = await conversation.send(DORA, group, photo=photo("meme"), date=earlier)
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


async def test_an_animated_sticker_reaches_the_model_as_its_preview_picture(
    conversation: Conversation, model: ScriptedModel
) -> None:
    group = Room(-1009, title="Stickers")
    conversation.meet(DORA, group)
    conversation.api.register_file("frog-thumb", png("green", (128, 128)))
    frog = {
        "file_id": "frog-tgs", "file_unique_id": "u-frog-tgs", "type": "regular", "width": 512, "height": 512,
        "is_animated": True, "is_video": False, "emoji": "🐸", "set_name": "frogs",
        "thumbnail": {"file_id": "frog-thumb", "file_unique_id": "u-frog-thumb", "width": 128, "height": 128},
    }  # fmt: skip
    posted = await conversation.send(DORA, group, sticker=frog)
    await conversation.settle(posted)

    def look(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (image,) = images_in(messages[-1:])
        assert image.media_type == "image/jpeg" and Image.open(io.BytesIO(image.data)).size == (128, 128)
        assert f"(the preview picture of the sticker from #{posted.message_id})" in str(messages[-1])
        return ModelResponse(parts=[TextPart("a smug frog")])

    model.reply(look, "a green square frog")
    asked = await conversation.send(DORA, group, "derp what's this", reply_to=posted.message_id)
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "a smug frog"
    await conversation.settle(asked)
    fetched = [call.fields["file_id"] for call in conversation.api.calls_of("getFile")]
    assert fetched == ["frog-thumb"]  # the Lottie file itself is never downloaded
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == posted.message_id))
        assert media is not None and media.description == "a green square frog"

    model.reply("still a frog")
    later = await conversation.send(DORA, group, "derp and now?")
    await conversation.expect_reply(later)
    window = str([p.content for m in model.requests[-1] for p in m.parts if isinstance(p, UserPromptPart)])
    assert "[sticker 🐸: a green square frog]" in window


def inspect_results(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "inspect_media"
    ]


async def test_inspect_media_watches_a_clip_with_its_sound_and_keeps_a_one_line_note(
    conversation: Conversation, model: ScriptedModel
) -> None:
    group = Room(-1031, title="Clips")
    conversation.meet(DORA, group)
    conversation.api.register_file("film", b"synthetic mp4 bytes")
    film = {
        "file_id": "film", "file_unique_id": "u-film", "width": 640, "height": 360, "duration": 42,
        "mime_type": "video/mp4", "file_size": 19,
    }  # fmt: skip
    posted = await conversation.send(DORA, group, video=film)
    await conversation.settle(posted)

    def watcher(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (clip,) = images_in(messages[-1:])
        assert clip.media_type == "video/mp4" and clip.data == b"synthetic mp4 bytes"  # the whole file
        assert "The question: what does he shout?" in str(messages[-1])
        return ModelResponse(
            parts=[TextPart('A man yells at a goose in a park.\n\n0:03 He shouts "give it back!"')],
            provider_details={"cost": 0.0006},
        )

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (result,) = inspect_results(messages)
        assert result.startswith(f"#{posted.message_id} (video 0:42, watched with sound):")
        assert '0:03 He shouts "give it back!"' in result
        assert images_in(messages) == []  # notes, not a still frame
        return ModelResponse(parts=[TextPart("he wants his sandwich back")])

    model.reply(tool_call("inspect_media", message_id=posted.message_id, question="what does he shout?"))
    model.reply(watcher, answer)
    asked = await conversation.send(DORA, group, f"derp what's going on in #{posted.message_id}?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "he wants his sandwich back"
    await conversation.settle(asked)
    assert DEFAULT_PINS[ModelRole.MEDIA] in model.pins  # the media pin watched it
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == posted.message_id))
        assert media is not None and media.description == "A man yells at a goose in a park."
        looks = list(await session.execute(select(ProviderCall.role, ProviderCall.cost_usd).where(
            ProviderCall.kind == "media")))  # fmt: skip
        assert [(role, float(cost)) for role, cost in looks] == [("media", 0.0006)]

    model.reply("the goose again")
    later = await conversation.send(DORA, group, "derp and the goose?")
    await conversation.expect_reply(later)
    window = str([p.content for m in model.requests[-1] for p in m.parts if isinstance(p, UserPromptPart)])
    assert "[video 0:42: A man yells at a goose in a park.]" in window


async def test_a_pdf_past_the_free_allowance_pages_or_a_pricey_look_says_why_and_shows_its_preview(
    conversation: Conversation, model: ScriptedModel
) -> None:
    group = Room(-1032, title="Papers")
    conversation.meet(DORA, group)
    pages = b"%PDF-1.4\n" + b"1 0 obj << /Type /Page >> endobj\n" * 12
    conversation.api.register_file("paper", pages)
    conversation.api.register_file("paper-thumb", png("white", (60, 80)))
    document = {
        "file_id": "paper", "file_unique_id": "u-paper", "file_name": "thesis.pdf",
        "mime_type": "application/pdf", "file_size": len(pages),
        "thumbnail": {"file_id": "paper-thumb", "file_unique_id": "u-paper-thumb", "width": 60, "height": 80},
    }  # fmt: skip
    posted = await conversation.send(DORA, group, document=document)
    await conversation.settle(posted)

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (result,) = inspect_results(messages)
        assert result.startswith(
            f"#{posted.message_id} (file thesis.pdf, 12 pages): not read — longer than the "
        )
        assert "(paid answers take up to 50 pages)" in result
        assert f"#{posted.message_id} (preview)" in result and len(images_in(messages)) == 1
        return ModelResponse(parts=[TextPart("too long for me today")])

    model.reply(tool_call("inspect_media", message_id=posted.message_id), "a title page", answer)
    asked = await conversation.send(DORA, group, f"derp summarize #{posted.message_id}")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert all(pin.model != "google/gemini-3.5-flash-lite" for pin in model.pins)

    # A short one fits the pages, but not this answer's money at a steep price.
    async with conversation.db.transaction() as session:
        await session.merge(ModelPin(role="media", pin=DEFAULT_PINS[ModelRole.MEDIA].model_dump(mode="json"),
                                     price_prompt=Decimal(900), price_completion=Decimal(900)))  # fmt: skip
    short = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n"
    conversation.api.register_file("memo", short)
    memo = await conversation.send(DORA, group, document={
        "file_id": "memo", "file_unique_id": "u-memo", "file_name": "memo.pdf", "mime_type": "application/pdf",
        "file_size": len(short)})  # fmt: skip
    await conversation.settle(memo)

    def broke(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (result,) = inspect_results(messages)
        assert "Derpcoins, more than this answer has left" in result
        return ModelResponse(parts=[TextPart("can't afford reading it")])

    model.reply(tool_call("inspect_media", message_id=memo.message_id), broke)
    asked = await conversation.send(DORA, group, f"derp read #{memo.message_id}")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        assert await session.scalar(select(ProviderCall.id).where(ProviderCall.role == "media")) is None


async def test_a_failed_listen_says_so_instead_of_guessing(
    conversation: Conversation, model: ScriptedModel
) -> None:
    group = Room(-1033, title="Tunes")
    conversation.meet(DORA, group)
    conversation.api.register_file("song", b"synthetic mp3 bytes")
    song = {
        "file_id": "song",
        "file_unique_id": "u-song",
        "duration": 5,
        "mime_type": "audio/mpeg",
        "file_size": 19,
    }
    posted = await conversation.send(DORA, group, audio=song)
    await conversation.settle(posted)

    def deaf(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (sound,) = images_in(messages[-1:])
        assert sound.media_type == "audio/mpeg" and NO_QUESTION in str(messages[-1])
        raise RuntimeError("provider exploded")

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        (result,) = inspect_results(messages)
        assert result == f"#{posted.message_id} (audio 0:05): not listened to — the viewer failed."
        return ModelResponse(parts=[TextPart("couldn't hear it")])

    model.reply(tool_call("inspect_media", message_id=posted.message_id), deaf, answer)
    asked = await conversation.send(DORA, group, f"derp what's playing in #{posted.message_id}?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "couldn't hear it"
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == posted.message_id))
        assert media is not None and media.description is None  # nothing heard, nothing stored
