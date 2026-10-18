import io

from PIL import Image
from sqlalchemy import select, update

from derp.agent.models import RunOutput
from derp.history.models import Message
from derp.money.models import RunFee
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_effects import jpeg, photo, results_of

MAX = Person(1901, "Max", "maxx")
ZOE = Person(1902, "Zoe")
DECK = Room(-1019, title="Deck")


async def test_derp_quote_this_sends_a_quote_sticker(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(MAX, DECK)
    conversation.meet(ZOE, DECK)
    conversation.api.register_file(f"profile-{MAX.id}", jpeg("orange"))  # Zoe has no photo: initials
    first = await conversation.send(MAX, DECK, "я сказал **это** 😂", entities=[
        {"type": "bold", "offset": 8, "length": 7},
    ])  # fmt: skip
    funny = await conversation.send(ZOE, DECK, "best line ever", reply_to=first.message_id)
    await conversation.settle(funny)
    model.reply(tool_call("make_quote"), ".")
    asked = await conversation.send(MAX, DECK, "derp, quote this", reply_to=funny.message_id)
    sent = await conversation.expect_reply(asked, method="sendSticker")
    await conversation.settle(asked)

    [attachment] = sent.attachments
    sticker = Image.open(io.BytesIO(attachment.data))
    assert sticker.format == "WEBP" and sticker.mode == "RGBA" and max(sticker.size) == 512
    assert len(attachment.data) <= 512 * 1024
    assert results_of(model, "make_quote")[0].startswith("Sent it as #")
    async with conversation.db.session() as session:
        assert await session.scalar(select(RunOutput.kind)) == "sticker"
        assert await session.scalar(select(RunFee.id)) is None  # no fee: light CPU
    assert conversation.api.calls_of("getUserProfilePhotos", after=asked.after)  # the avatar, via the gateway


async def test_a_quote_of_the_last_messages_as_an_image_leaves_out_forgotten_ones(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(MAX, DECK)
    conversation.meet(ZOE, DECK)
    conversation.api.register_file("pic", jpeg("teal"))
    one = await conversation.send(MAX, DECK, "look at this")
    two = await conversation.send(MAX, DECK, photo=photo("pic", jpeg("teal")))
    secret = await conversation.send(ZOE, DECK, "my secret")
    await conversation.settle(secret)
    async with conversation.db.transaction() as session:
        await session.execute(
            update(Message)
            .where(Message.chat_id == DECK.id, Message.message_id == secret.message_id)
            .values(text=None, deleted_at=Message.date)
        )
    ids = [one.message_id, two.message_id, secret.message_id]
    model.reply(tool_call("make_quote", message_ids=ids, output="image"), ".")
    asked = await conversation.send(ZOE, DECK, "derp quote those as a picture")
    sent = await conversation.expect_reply(asked, method="sendDocument")
    await conversation.settle(asked)

    picture = Image.open(io.BytesIO(sent.attachments[0].data))
    assert picture.format == "PNG" and picture.mode == "RGBA"
    result = results_of(model, "make_quote")[0]
    assert result.startswith("Sent it as #") and f"#{secret.message_id}" in result


async def test_nothing_to_quote_is_explained_to_the_model(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(MAX, DECK)
    model.reply(tool_call("make_quote"), "reply to something")
    asked = await conversation.send(MAX, DECK, "derp quote")
    await conversation.settle(asked)
    assert "Which messages?" in results_of(model, "make_quote")[0]
    assert conversation.api.calls_of("sendSticker", after=asked.after) == []


async def test_messages_of_another_chat_are_never_quoted(
    conversation: Conversation, model: ScriptedModel
) -> None:
    other = Room(-1020, title="Elsewhere")
    conversation.meet(ZOE, other)
    conversation.meet(MAX, DECK)
    await conversation.send(ZOE, other, "hi")
    await conversation.send(ZOE, other, "so")
    theirs = await conversation.send(ZOE, other, "private stuff")
    await conversation.settle(theirs)  # ids 1-3 there; this chat only has the ask (id 1)
    model.reply(tool_call("make_quote", message_ids=[theirs.message_id]), "nope")
    asked = await conversation.send(MAX, DECK, "derp quote that")
    await conversation.settle(asked)
    assert "None of those messages are stored here" in results_of(model, "make_quote")[0]
    assert conversation.api.calls_of("sendSticker", after=asked.after) == []
