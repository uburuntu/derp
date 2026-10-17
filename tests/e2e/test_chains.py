"""Media chains: intermediate results are kept, only the end result is posted, money follows delivery."""

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from derp.agent.models import AgentRun, RunOutput
from derp.inference.models import ProviderCall
from derp.money.models import RunFee
from derp.money.wallets import balance
from derp.tools import background, effects
from derp.tools.workers import EffectError
from tests.e2e.harness import Conversation
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_effects import results_of
from tests.e2e.test_images import JO, FakeImages, fund

IMAGE_CREDITS = 48  # ceil(0.04 × 1.2 / 0.001), FakeImages' price


def cutout_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def cutouts(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Background removal without the model weights: every picture comes back as a transparent PNG."""
    seen: list[bytes] = []

    async def remove(data: bytes, _model: Path) -> bytes:
        seen.append(data)
        return cutout_png()

    monkeypatch.setattr(background, "available", lambda _model: True)
    monkeypatch.setattr(effects, "remove_background", remove)
    return seen


async def drawing(conversation: Conversation) -> tuple[FakeImages, int]:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    return images, await fund(conversation, 650)


async def test_draw_cut_out_and_sticker_posts_only_the_sticker(
    conversation: Conversation, model: ScriptedModel, cutouts: list[bytes]
) -> None:
    images, wallet_id = await drawing(conversation)
    model.reply(
        tool_call("send_dice"),  # one slot taken first: the chain still fits in the one left
        tool_call("generate_image", prompt="a cat", final=False),
        tool_call("remove_background", from_result="r1", final=False),
        tool_call("make_sticker", from_result="r2"),
        ".",
    )
    asked = await conversation.send(JO, text="draw a cat, cut it out, make it a sticker")
    sticker = await conversation.expect_reply(asked, method="sendSticker")
    await conversation.settle(asked)
    assert images.prompts == ["a cat"] and len(cutouts) == 1
    assert Image.open(io.BytesIO(sticker.attachments[0].data)).mode == "RGBA"
    for method in ("sendPhoto", "sendDocument", "sendRichMessage"):
        assert conversation.api.calls_of(method, after=asked.after) == [], method
    assert 'Kept it as result "r1"' in results_of(model, "generate_image")[0]
    assert results_of(model, "make_sticker")[0].startswith("Sent it as #")
    fee = conversation.settings.effect_credits
    async with conversation.db.session() as session:
        assert sorted(await session.scalars(select(RunOutput.kind))) == ["dice", "sticker"]
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == IMAGE_CREDITS + fee
        [call] = await session.scalars(select(ProviderCall).where(ProviderCall.kind == "image"))
        assert (call.run_id, call.funding) == (run.id, "paid")
        assert list(await session.scalars(select(RunFee.item))) == ["cutout"]
        assert await balance(session, wallet_id) == 650 - IMAGE_CREDITS - fee


async def test_a_kept_result_nobody_used_is_posted_before_the_answer(
    conversation: Conversation, model: ScriptedModel
) -> None:
    _images, _wallet = await drawing(conversation)
    model.reply(tool_call("generate_image", prompt="a dog", final=False), "here's your dog")
    asked = await conversation.send(JO, text="draw a dog")
    reply = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    [photo] = conversation.api.calls_of("sendPhoto", after=asked.after)
    assert photo.sequence < reply.sequence and reply["rich_message"]["markdown"] == "here's your dog"
    async with conversation.db.session() as session:
        assert sorted(await session.scalars(select(RunOutput.kind))) == ["image", "message"]
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == IMAGE_CREDITS


async def test_a_step_that_fails_mid_chain_still_posts_what_was_made(
    conversation: Conversation, model: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_data: bytes, _model: Path) -> bytes:
        raise EffectError("background removal took too long")

    monkeypatch.setattr(background, "available", lambda _model: True)
    monkeypatch.setattr(effects, "remove_background", broken)
    _images, _wallet = await drawing(conversation)
    model.reply(
        tool_call("generate_image", prompt="a cat", final=False),
        tool_call("remove_background", from_result="r1"),
        ".",
    )
    asked = await conversation.send(JO, text="draw a cat and cut it out")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert "nothing was charged" in results_of(model, "remove_background")[0]
    assert conversation.api.calls_of("sendRichMessage", after=asked.after) == []  # the photo is the answer
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == IMAGE_CREDITS  # the image, no fee
        assert await session.scalar(select(RunFee.id)) is None


async def test_a_refused_end_result_leaves_its_source_to_be_posted(
    conversation: Conversation, model: ScriptedModel
) -> None:
    _images, _wallet = await drawing(conversation)
    conversation.api.fail_next("sendSticker", error_code=400, description="Bad Request: STICKER_INVALID")
    model.reply(
        tool_call("generate_image", prompt="a cat", final=False),
        tool_call("make_sticker", from_result="r1"),
        ".",
    )
    asked = await conversation.send(JO, text="a cat sticker")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert 'Result "r1" is still kept' in results_of(model, "make_sticker")[0]
    async with conversation.db.session() as session:
        assert list(await session.scalars(select(RunOutput.kind))) == ["image"]
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == IMAGE_CREDITS


async def test_nothing_reaching_the_chat_absorbs_the_image(
    conversation: Conversation, model: ScriptedModel
) -> None:
    _images, wallet_id = await drawing(conversation)
    conversation.api.fail_next("sendPhoto", error_code=400, description="Bad Request: PHOTO_INVALID")
    model.reply(tool_call("generate_image", prompt="a cat", final=False), "it's coming")
    asked = await conversation.send(JO, text="draw a cat")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        assert list(await session.scalars(select(RunOutput.kind))) == ["message"]
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == 0
        [call] = await session.scalars(select(ProviderCall).where(ProviderCall.kind == "image"))
        assert (call.run_id, call.funding) == (None, "creator")
        assert await balance(session, wallet_id) == 650


async def test_an_unconfirmed_end_result_uses_up_its_source(
    conversation: Conversation, model: ScriptedModel
) -> None:
    _images, _wallet = await drawing(conversation)
    conversation.api.fail_next("sendSticker", error_code=502, description="Bad Gateway")
    model.reply(
        tool_call("generate_image", prompt="a cat", final=False),
        tool_call("make_sticker", from_result="r1"),
        "sent, probably",
    )
    asked = await conversation.send(JO, text="a cat sticker")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "didn't confirm" in results_of(model, "make_sticker")[0]
    assert conversation.api.calls_of("sendPhoto", after=asked.after) == []  # it may have arrived: no repost
    async with conversation.db.session() as session:
        [call] = await session.scalars(select(ProviderCall).where(ProviderCall.kind == "image"))
        assert (call.run_id, call.funding) == (None, "creator")


async def test_an_image_needing_approval_is_not_chained(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 150)  # the image is over a quarter of the balance: it needs a press
    model.reply(tool_call("generate_image", prompt="a cat", final=False), "it needs a press")
    asked = await conversation.send(JO, text="draw a cat and make it a sticker")
    await conversation.settle(asked)
    assert "can't be part of a chain" in results_of(model, "generate_image")[0] and images.prompts == []
