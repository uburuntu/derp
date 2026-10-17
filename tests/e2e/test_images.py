import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from PIL import Image
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select, update

from derp.agent.approvals import close_expired
from derp.agent.models import AgentRun, Approval, RunOutput
from derp.billing.menu import BuyAction
from derp.inference.api import GeneratedImage, OpenRouterError
from derp.inference.models import ProviderCall
from derp.money.wallets import balance, claw_back, credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_effects import jpeg, results_of

JO = Person(1501, "Jo")


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "gold").save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class FakeImages:
    """Stands in for OpenRouterAPI: images cost $0.04 each."""

    prompts: list[str] = field(default_factory=list[str])
    references: list[list[tuple[bytes, str]]] = field(default_factory=list[list[tuple[bytes, str]]])

    async def generate_image(
        self, model: str, prompt: str, *, references: Sequence[tuple[bytes, str]] = (), **_: object
    ) -> GeneratedImage:
        self.prompts.append(prompt)
        self.references.append(list(references))
        return GeneratedImage(png(), "image/png", model, Decimal("0.04"))

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
    [deleted] = conversation.api.calls_of("deleteMessage", after=after)  # the image arrived: the prompt goes
    assert int(deleted["message_id"]) == prompt_id and (JO.id, prompt_id) not in conversation.api.messages


class BrokenImages(FakeImages):
    async def generate_image(self, model: str, prompt: str, **_: object) -> GeneratedImage:
        self.prompts.append(prompt)
        raise OpenRouterError(502, "upstream down")


async def test_an_approved_image_that_fails_turns_its_prompt_into_the_failure_note(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = BrokenImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    wallet_id = await fund(conversation, 150)
    prompt_id, run = await ask_for_a_dragon(conversation, model)
    async with conversation.db.session() as session:
        before = await balance(session, wallet_id)
    after = conversation.seq
    await conversation.press(JO, JO.id, prompt_id, run)
    assert set(images.prompts) == {"a dragon"}  # every pinned model was tried
    assert conversation.api.calls_of("deleteMessage", after=after) == []
    assert conversation.api.calls_of("sendRichMessage", after=after) == []  # no second message
    note = conversation.api.calls_of("editMessageText", after=after)[-1]
    assert int(note["message_id"]) == prompt_id
    assert note["rich_message"]["markdown"] == "The image didn't come out — nothing was charged."
    assert "reply_markup" not in conversation.api.message(JO.id, prompt_id)
    async with conversation.db.session() as session:
        approved = await session.scalar(select(AgentRun).where(AgentRun.trigger == "callback"))
        assert approved is not None and approved.status == "failed" and approved.charged_credits == 0
        assert await balance(session, wallet_id) == before


async def test_an_approved_image_falls_through_to_the_payer_who_can_cover_it(
    conversation: Conversation, model: ScriptedModel
) -> None:
    """A group wallet that covers a turn but not the image: the asker's own Derpcoins pay for it."""
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    club = Room(-1503, title="Club")
    conversation.meet(JO, club)
    async with conversation.db.transaction() as session:
        group_wallet = (await wallet(session, "chat", club.id)).id
        await credit(session, group_wallet, "stars:club", 20, "purchase")
    own_wallet = await fund(conversation, 650)
    model.reply(tool_call("generate_image", prompt="a party hat"), ".")
    asked = await conversation.send(JO, club, "derp draw a party hat")
    prompt = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    run_button, _cancel = prompt["reply_markup"]["inline_keyboard"][0]
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == club.id)
    after = conversation.seq
    await conversation.press(JO, club.id, prompt_id, run_button["callback_data"])
    assert conversation.api.calls_of("sendPhoto", after=after) and images.prompts == ["a party hat"]
    async with conversation.db.session() as session:
        approved = await session.scalar(select(AgentRun).where(AgentRun.trigger == "callback"))
        assert approved is not None and approved.wallet_id == own_wallet and approved.charged_credits == 48
        assert await balance(session, own_wallet) == 650 - 48


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
    text = "Cancelled — the image wasn't made, and nothing was charged."
    assert closed["rich_message"]["markdown"] == text and images.prompts == []
    async with conversation.db.session() as session:
        assert await session.scalar(select(Approval.state)) == "denied"
        assert await session.scalar(select(AgentRun.id).where(AgentRun.trigger == "callback")) is None
        asking = await session.scalar(select(AgentRun))
        left = await balance(session, (await wallet(session, "user", JO.id)).id)
    assert asking is not None and left == 150 - asking.charged_credits  # only the asking turn's tokens


async def test_a_profile_photo_gets_an_ai_edit_in_one_call(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 650)
    avatar = jpeg("orange")
    conversation.api.register_file(f"profile-{JO.id}", avatar)
    model.reply(tool_call("generate_image", prompt="add a fun party hat", profile_photo=True), ".")
    asked = await conversation.send(JO, text="add me a fun hat to my profile picture")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert [data for data, _ in images.references[0]] == [avatar]
    assert conversation.api.calls_of("setChatPhoto") == []  # the person sets it themselves


async def test_a_member_is_drawn_by_name(conversation: Conversation, model: ScriptedModel) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 650)
    club, vasya = Room(-1503, title="Club"), Person(1504, "Вася", username="vasyan")
    conversation.meet(JO, club)
    conversation.meet(vasya, club)
    await conversation.settle(await conversation.send(vasya, club, "привет"))
    avatar = jpeg("green")
    conversation.api.register_file(f"profile-{vasya.id}", avatar)
    model.reply(tool_call("generate_image", prompt="this person as a knight", person="Вася"), ".")
    asked = await conversation.send(JO, club, "дерп нарисуй Васю рыцарем")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert [data for data, _ in images.references[0]] == [avatar]


async def test_the_chat_photo_is_a_source_for_edits(conversation: Conversation, model: ScriptedModel) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 650)
    club = Room(-1502, title="Club")
    conversation.meet(JO, club)
    logo = jpeg("purple")
    conversation.api.register_file(f"chatphoto-{club.id}", logo)
    model.reply(tool_call("generate_image", prompt="give it a santa hat", chat_photo=True), ".")
    asked = await conversation.send(JO, club, "derp put a santa hat on our chat picture")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    assert [data for data, _ in images.references[0]] == [logo]


def meme_of_the_sent_image(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    """The next tool takes the picture the previous one sent, by the id in its answer."""
    sent = next(
        str(part.content)
        for message in reversed(messages)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "generate_image"
    )
    match = re.search(r"#(\d+)", sent)
    assert match, sent
    args = {"text": "me after the party", "message_id": int(match.group(1))}
    return ModelResponse(parts=[ToolCallPart("make_meme", args, tool_call_id="meme-call")])


async def test_a_drawn_image_feeds_the_meme_tool(conversation: Conversation, model: ScriptedModel) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 650)
    model.reply(tool_call("generate_image", prompt="a cat in a party hat"), meme_of_the_sent_image, ".")
    asked = await conversation.send(JO, text="draw a party cat and make a meme of it")
    await conversation.settle(asked)
    photos = conversation.api.calls_of("sendPhoto", after=asked.after)
    assert len(photos) == 2 and images.prompts == ["a cat in a party hat"]
    meme = results_of(model, "make_meme")
    assert meme and meme[0].startswith("Sent"), meme


async def ask_for_a_dragon(conversation: Conversation, model: ScriptedModel) -> tuple[int, str]:
    """An image request that has to ask first: the prompt's message id and its Run button's data."""
    model.reply(tool_call("generate_image", prompt="a dragon"), ".")
    asked = await conversation.send(JO, text="draw a dragon")
    prompt = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == JO.id)
    return prompt_id, prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]


async def test_expired_approval_prompts_lose_their_buttons(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 150)
    pressed, run = await ask_for_a_dragon(conversation, model)
    swept, _ = await ask_for_a_dragon(conversation, model)
    async with conversation.db.transaction() as session:
        await session.execute(update(Approval).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))

    after = conversation.seq
    await conversation.press(JO, JO.id, pressed, run)
    refusal = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert "expired" in refusal["text"] and refusal["show_alert"] and images.prompts == []
    shown = conversation.api.message(JO.id, pressed)
    assert "reply_markup" not in shown and "expired" in str(shown["rich_message"])

    assert conversation.services is not None
    bot = conversation.services.bot
    after = conversation.seq
    assert await close_expired(conversation.db, bot, datetime.now(UTC)) == 1  # the one nobody pressed
    [closed] = conversation.api.calls_of("editMessageText", after=after)
    assert int(closed["message_id"]) == swept and "expired" in closed["rich_message"]["markdown"]
    assert "reply_markup" not in conversation.api.message(JO.id, swept)
    async with conversation.db.session() as session:
        assert set(await session.scalars(select(Approval.state))) == {"expired"}
    assert await close_expired(conversation.db, bot, datetime.now(UTC)) == 0


async def test_an_approval_that_cant_be_paid_any_more_offers_buying(
    conversation: Conversation, model: ScriptedModel
) -> None:
    images = FakeImages()
    conversation.overrides["api"] = images
    await conversation.restart()
    await fund(conversation, 150)
    prompt_id, run = await ask_for_a_dragon(conversation, model)
    async with conversation.db.transaction() as session:
        await claw_back(session, "stars:jo", "refund:jo")
    after = conversation.seq
    await conversation.press(JO, JO.id, prompt_id, run)
    notice = conversation.api.message(JO.id, prompt_id)  # the prompt itself becomes the note
    assert "nothing was charged" in str(notice["rich_message"]) and images.prompts == []
    [[buy]] = notice["reply_markup"]["inline_keyboard"]
    assert buy["callback_data"] == BuyAction(target="user").pack()
    assert not conversation.api.calls_of("sendRichMessage", after=after)

    after = conversation.seq
    await conversation.press(JO, JO.id, prompt_id, buy["callback_data"])
    [menu] = conversation.api.calls_of("editMessageText", after=after)
    assert int(menu["message_id"]) == prompt_id and "Your balance" in menu["rich_message"]["markdown"]
