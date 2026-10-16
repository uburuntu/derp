from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select

from derp.agent.models import AgentRun, Approval, RunOutput
from derp.inference.api import GeneratedImage
from derp.inference.models import ProviderCall
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person
from tests.e2e.model import ScriptedModel, tool_call

JO = Person(1501, "Jo")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class FakeImages:
    """Stands in for OpenRouterAPI: images cost $0.04 each."""

    prompts: list[str] = field(default_factory=list[str])

    async def generate_image(self, model: str, prompt: str, **_: object) -> GeneratedImage:
        self.prompts.append(prompt)
        return GeneratedImage(PNG, "image/png", model, Decimal("0.04"))

    async def close(self) -> None:
        return None


async def fund(conversation: Conversation, credits: int) -> int:
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", JO.id)).id
        await credit(session, wallet_id, "stars:jo", credits, "purchase")
    return wallet_id


async def test_a_standard_image_goes_out_and_is_charged(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    wallet_id = await fund(conversation, 650)
    model.reply(tool_call("generate_image", prompt="a cat in a tiny hat"), ".")
    asked = await conversation.send(JO, text="draw a cat in a hat")
    photo = await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert images.prompts == ["a cat in a tiny hat"] and photo.attachments
    assert conversation.api.calls_of("sendRichMessage", after=asked.after) == []  # "." sends nothing else
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == 48  # ceil(0.04 × 1.2 / 0.001)
        assert await session.scalar(select(RunOutput.kind)) == "image"
        assert await balance(session, wallet_id) == 650 - 48


async def test_an_image_over_a_quarter_of_the_balance_asks_first(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    wallet_id = await fund(conversation, 150)
    model.reply(tool_call("generate_image", prompt="a dragon"), ".")
    asked = await conversation.send(JO, text="draw a dragon")
    prompt = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "costs about **54** Derpcoins" in prompt["rich_message"]["markdown"] and images.prompts == []
    run_button, _cancel = prompt["reply_markup"]["inline_keyboard"][0]
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == JO.id)

    stranger = Person(1502, "Kim")
    conversation.meet(stranger)
    after = conversation.seq
    await conversation.press(stranger, JO.id, prompt_id, run_button["callback_data"])
    refusal = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert "Only the person who asked" in str(refusal["text"]) and images.prompts == []

    after = conversation.seq
    await conversation.press(JO, JO.id, prompt_id, run_button["callback_data"])
    photo = conversation.api.calls_of("sendPhoto", after=after)[-1]
    assert int(photo["reply_parameters"]["message_id"]) == asked.message_id and images.prompts == ["a dragon"]
    async with conversation.db.session() as session:
        assert await session.scalar(select(Approval.state)) == "approved"
        approved = await session.scalar(select(AgentRun).where(AgentRun.trigger == "callback"))
        assert approved is not None and approved.charged_credits == 48 and approved.status == "completed"
        left = await balance(session, wallet_id)
        asking = await session.scalar(select(AgentRun).where(AgentRun.trigger != "callback"))
    assert asking is not None and left == 150 - 48 - asking.charged_credits  # the asking turn pays its tokens

    closed = conversation.api.calls_of("editMessageText", after=after)[-1]
    assert closed["rich_message"]["markdown"] == "On it — drawing…" and "reply_markup" not in closed.fields


async def test_images_need_paid_derpcoins(conversation: Conversation, model: ScriptedModel) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    model.reply(tool_call("generate_image", prompt="a dog"), "Images need Derpcoins — /buy gets some.")
    asked = await conversation.send(JO, text="draw a dog")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert images.prompts == []  # sponsored: the tool declines
    async with conversation.db.session() as session:
        assert await session.scalar(select(ProviderCall.id).where(ProviderCall.kind == "image")) is None


async def test_cancelling_an_expensive_image_charges_nothing_for_it(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 150)
    model.reply(tool_call("generate_image", prompt="a castle"), ".")
    asked = await conversation.send(JO, text="draw a castle")
    prompt = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    cancel = prompt["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == JO.id)
    after = conversation.seq
    await conversation.press(JO, JO.id, prompt_id, cancel)
    closed = conversation.api.calls_of("editMessageText", after=after)[-1]
    text = "Cancelled — the image wasn't made, and it costs nothing."
    assert closed["rich_message"]["markdown"] == text and images.prompts == []
    async with conversation.db.session() as session:
        assert await session.scalar(select(Approval.state)) == "denied"
        assert await session.scalar(select(AgentRun.id).where(AgentRun.trigger == "callback")) is None
        asking = await session.scalar(select(AgentRun))
        left = await balance(session, (await wallet(session, "user", JO.id)).id)
    assert asking is not None and left == 150 - asking.charged_credits  # only the asking turn's tokens
