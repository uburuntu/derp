"""A revision keeps the old link, spends only on confirmed delivery, and becomes the visible history."""

from datetime import UTC, datetime
from typing import Any

from aiogram.types import Message as TgMessage
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select

from derp.agent.models import RunOutput
from derp.agent.runs import runs_for_output
from derp.history.capture import store_message
from derp.history.models import Digest, Message, MessageMedia
from derp.inference.models import ProviderCall
from derp.money.wallets import balance
from tests.e2e.harness import Conversation
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_chains import IMAGE_CREDITS, drawing
from tests.e2e.test_images import JO, png


async def first_answer(conversation: Conversation, model: ScriptedModel) -> int:
    model.reply("Dinner at eight")
    first = await conversation.send(JO, text="When is dinner?")
    await conversation.settle(first)
    async with conversation.db.session() as session:
        message_id = await session.scalar(select(Message.message_id).where(Message.is_derp))
    assert message_id is not None
    return message_id


async def test_generated_media_can_replace_text_then_its_caption_can_change(
    conversation: Conversation, model: ScriptedModel
) -> None:
    _images, wallet_id = await drawing(conversation)
    message_id = await first_answer(conversation, model)
    async with conversation.db.transaction() as session:
        session.add(
            Digest(
                chat_id=JO.id,
                thread_id=0,
                day=datetime.now(UTC).date(),
                first_message_id=message_id,
                last_message_id=message_id,
                summary="Dinner at eight",
                model="test",
                message_count=1,
            )
        )
    model.reply(
        tool_call("generate_image", prompt="a dinner table", final=False),
        tool_call(
            "message_action", action="edit", message_id=message_id, media="r1", caption="Dinner at nine"
        ),
        ".",
    )
    replace = await conversation.send(JO, text="Turn your answer into a photo", reply_to=message_id)
    await conversation.settle(replace)
    (edit,) = conversation.api.calls_of("editMessageMedia", after=replace.after)
    assert int(edit["message_id"]) == message_id
    assert conversation.api.calls_of("sendPhoto", after=replace.after) == []
    assert conversation.api.calls_of("sendRichMessage", after=replace.after) == []
    async with conversation.db.session() as session:
        row = await session.get(Message, (JO.id, message_id))
        assert row and (row.kind, row.text, row.is_derp) == ("photo", "Dinner at nine", True)
        assert await session.get(MessageMedia, (JO.id, message_id, 0)) is not None
        assert await session.scalar(select(Digest.stale)) is True
        (original, origin_edited), (revision, revision_edited) = await runs_for_output(
            session, JO.id, message_id
        )
        assert not origin_edited and revision_edited and original.id < revision.id
        assert revision.charged_credits == IMAGE_CREDITS
        assert (
            await session.scalar(select(ProviderCall.run_id).where(ProviderCall.kind == "image"))
            == revision.id
        )
        assert await balance(session, wallet_id) == 650 - IMAGE_CREDITS

    model.reply(
        tool_call("message_action", action="edit", message_id=message_id, caption="Dinner at ten"), "Updated"
    )
    fix = await conversation.send(
        JO, text="Actually change the caption to Dinner at ten", reply_to=message_id
    )
    await conversation.settle(fix)
    (ack,) = conversation.api.calls_of("sendRichMessage", after=fix.after)
    assert ack["reply_parameters"]["message_id"] == message_id
    model.reply("Ten")
    read = await conversation.send(JO, text="When is it again?")
    await conversation.settle(read)
    visible = [
        p.content
        for m in model.requests[-1]
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, TextPart)
    ]
    assert any("Dinner at ten" in t for t in visible)
    assert all("Dinner at eight" not in t for t in visible)
    async with conversation.db.session() as session:
        assert [revised for _, revised in await runs_for_output(session, JO.id, message_id)] == [
            False,
            True,
            True,
        ]


async def test_definite_refusal_posts_linked_replacement_and_keeps_original(
    conversation: Conversation, model: ScriptedModel
) -> None:
    message_id = await first_answer(conversation, model)
    conversation.api.fail_next(
        "editMessageText", error_code=400, description="Bad Request: message can't be edited"
    )
    model.reply(tool_call("message_action", action="edit", message_id=message_id, text="Dinner at nine"), ".")
    asked = await conversation.send(JO, text="Please correct the old answer", reply_to=message_id)
    await conversation.settle(asked)
    (replacement,) = conversation.api.calls_of("sendRichMessage", after=asked.after)
    assert replacement["reply_parameters"]["message_id"] == message_id
    assert not replacement["reply_parameters"]["allow_sending_without_reply"]
    assert "Dinner at nine" in replacement["rich_message"]["markdown"]
    async with conversation.db.session() as session:
        original = await session.get(Message, (JO.id, message_id))
        assert original and original.text == "Dinner at eight"
        assert len(await runs_for_output(session, JO.id, message_id)) == 1


async def test_uncertain_media_edit_neither_replays_nor_flushes_kept_image(
    conversation: Conversation, model: ScriptedModel
) -> None:
    _images, wallet_id = await drawing(conversation)
    message_id = await first_answer(conversation, model)
    conversation.api.fail_next("editMessageMedia", error_code=502, description="Bad Gateway")
    model.reply(
        tool_call("generate_image", prompt="dinner", final=False),
        tool_call("message_action", action="edit", message_id=message_id, media="r1"),
        tool_call("message_action", action="edit", message_id=message_id, text="Try again"),
        "Delivery uncertain",
    )
    asked = await conversation.send(JO, text="Revise that with a photo")
    await conversation.settle(asked)
    assert len(conversation.api.calls_of("editMessageMedia", after=asked.after)) == 1
    assert not conversation.api.calls_of("editMessageText", after=asked.after)
    assert not conversation.api.calls_of("sendPhoto", after=asked.after)
    async with conversation.db.session() as session:
        assert await balance(session, wallet_id) == 650
        assert await session.scalar(select(RunOutput.id).where(RunOutput.kind == "edit")) is None
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "image"))
        assert call and call.funding == "creator" and call.run_id is None


async def stored(conversation: Conversation, *, own: bool, **content: Any) -> int:
    api = conversation.api
    message_id = api.take_message_id(JO.id)
    payload = {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": api.chats[JO.id],
        "from": api.bot_user if own else JO.payload(),
        **content,
    }
    api.register_message(payload)
    async with conversation.db.transaction() as session:
        await store_message(
            session,
            TgMessage.model_validate(payload),
            capture="derp" if own else "ambient",
            bot_id=int(api.bot_user["id"]),
        )
    return message_id


async def test_visual_album_accepts_live_photo_but_posts_document_as_linked_replacement(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await first_answer(conversation, model)
    photo = {"file_id": "still", "file_unique_id": "u-still", "width": 64, "height": 64}
    conversation.api.register_file("still", png())
    album = await stored(conversation, own=True, photo=[photo], caption="Dinner", media_group_id="group")
    live = await stored(
        conversation,
        own=False,
        photo=[photo],  # Telegram always provides the legacy still; LivePhoto.photo itself is optional
        live_photo={
            "file_id": "clip",
            "file_unique_id": "u-clip",
            "width": 64,
            "height": 64,
            "duration": 2,
        },
    )
    conversation.api.register_file("pdf", b"%PDF-1.7")
    doc = await stored(
        conversation,
        own=False,
        document={"file_id": "pdf", "file_unique_id": "u-pdf", "file_name": "menu.pdf"},
    )
    model.reply(tool_call("message_action", action="edit", message_id=album, media=f"#{live}"), ".")
    asked = await conversation.send(JO, text="Use my live photo instead")
    await conversation.settle(asked)
    (applied,) = conversation.api.calls_of("editMessageMedia", after=asked.after)
    assert applied["media"] == {"type": "live_photo", "media": "clip", "photo": "still", "caption": "Dinner"}
    async with conversation.db.session() as session:
        row = await session.get(Message, (JO.id, album))
        assert row and row.kind == "live_photo" and row.media_group_id == "group"
    model.reply(
        tool_call("message_action", action="edit", message_id=album, media=f"#{doc}", caption="Menu"), "."
    )
    asked = await conversation.send(JO, text="Use the PDF there instead")
    await conversation.settle(asked)
    assert not conversation.api.calls_of("editMessageMedia", after=asked.after)
    (replacement,) = conversation.api.calls_of("sendDocument", after=asked.after)
    assert replacement["document"] == "pdf" and replacement["reply_parameters"]["message_id"] == album
    assert replacement["caption"].startswith("Menu")


async def test_human_messages_and_control_cards_cannot_be_edited(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await first_answer(conversation, model)
    human = await stored(conversation, own=False, text="original")
    card = await stored(
        conversation,
        own=True,
        text="Purchase",
        reply_markup={
            "inline_keyboard": [[{"text": "Buy", "callback_data": "pay"}]],
        },
    )
    model.reply(
        tool_call("message_action", action="edit", message_id=human, text="changed"),
        tool_call("message_action", action="edit", message_id=card, text="changed"),
        "Those messages cannot be edited.",
    )
    asked = await conversation.send(JO, text="Change both", reply_to=human)
    await conversation.settle(asked)
    assert not conversation.api.calls_of("editMessageText", after=asked.after)
    assert len(conversation.api.calls_of("sendRichMessage", after=asked.after)) == 1
    assert conversation.api.message(JO.id, card)["reply_markup"]["inline_keyboard"]


async def test_no_op_stays_quiet_without_a_revision_or_replacement(
    conversation: Conversation, model: ScriptedModel
) -> None:
    answer = await first_answer(conversation, model)
    conversation.api.fail_next(
        "editMessageText", error_code=400, description="Bad Request: message is not modified"
    )
    model.reply(tool_call("message_action", action="edit", message_id=answer, text="Dinner at eight"), ".")
    asked = await conversation.send(JO, text="Make sure it still says dinner at eight", reply_to=answer)
    await conversation.settle(asked)
    assert not conversation.api.calls_of("sendRichMessage", after=asked.after)
    async with conversation.db.session() as session:
        assert [revised for _, revised in await runs_for_output(session, JO.id, answer)] == [False]
