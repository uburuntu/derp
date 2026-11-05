"""Contributed keys: plugging a ChatGPT key in privately, an admin turning it on, the key answering the group,
and the chat falling back to its usual funding when the key refuses or stops working."""

import io
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from PIL import Image
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.history.models import Message as StoredMessage
from derp.inference.api import Credential, GeneratedImage, OpenRouterError, SubKey
from derp.inference.models import ProviderCall
from derp.keys.flows import chat_ref
from derp.keys.models import ContributedKey, KeyChat
from derp.money.wallets import credit, wallet
from tests.e2e.fake_api import Call, Failure
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_effects import jpeg

ANA = Person(1701, "Ana")  # an admin of the group
BO = Person(1702, "Bo")  # has a ChatGPT Pro plan
CREW = Room(-1700, title="Crew")
PASTED = "sk-proj-" + "Zx9_" * 12
MENTION = [{"type": "mention", "offset": 0, "length": 10}]


@dataclass
class FakeManagement:
    calls: list[str] = field(default_factory=list[str])

    async def create_key(self, name: str, *, limit_usd: float) -> SubKey:
        self.calls.append("create_key")
        return SubKey(secret="sk-or-v1-bo", hash="b" * 64)

    async def delete_key(self, key_hash: str) -> None:
        self.calls.append("delete_key")

    async def register(
        self, provider: str, key: str, *, name: str, sub_key_hash: str, models: Any
    ) -> Credential:
        self.calls.append("register")
        return Credential(id="cred-bo", label="sk-proj-...Zx9_")

    async def allow_models(self, credential_id: str, models: Any) -> None:
        self.calls.append("allow_models")

    async def unregister(self, credential_id: str) -> None:
        self.calls.append("unregister")

    async def close(self) -> None:
        return None


@dataclass
class FakeKeysAPI:
    """Stands in for OpenRouterAPI: the plug-in ping succeeds unless ``status`` says otherwise; images drawn on
    a key cost its owner $0.05 each and are recorded in ``drawn``."""

    status: int | None = None
    drawn: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def ping(self, model: str, provider: str, *, api_key: str) -> None:
        if self.status:
            raise OpenRouterError(self.status, "no")

    async def generate_image(
        self, model: str, prompt: str, *, references: Sequence[tuple[bytes, str]] = (), **options: Any
    ) -> GeneratedImage:
        self.drawn.append({"model": model, "prompt": prompt, "references": len(references), **options})
        buffer = io.BytesIO()
        Image.new("RGB", (1024, 1024), "green").save(buffer, format="PNG")
        return GeneratedImage(buffer.getvalue(), "image/png", model, Decimal(0), upstream_usd=Decimal("0.05"))

    async def close(self) -> None:
        return None


def markdown(call: Call) -> str:
    return str(call["rich_message"]["markdown"])


def buttons(call: Call) -> list[dict[str, Any]]:
    return [b for row in call["reply_markup"]["inline_keyboard"] for b in row]


def data_of(call: Call, label: str) -> str:
    return next(str(b["callback_data"]) for b in buttons(call) if label in b["text"])


async def latest(conversation: Conversation, method: str, chat_id: int, after: int) -> Call:
    return await conversation.api.wait_call(method, after=after, where=lambda c: int(c["chat_id"]) == chat_id)


def said(conversation: Conversation, chat_id: int, after: int) -> list[Call]:
    """What Derp posted in a chat since ``after``, the group's one-time onboarding notice aside."""
    return [
        c
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if int(c["chat_id"]) == chat_id and "Derp is here" not in markdown(c)
    ]


def newest(conversation: Conversation, chat_id: int) -> int:
    """The id of the newest live message in a chat (Derp's, right after it sent one)."""
    return max(mid for (cid, mid) in conversation.api.messages if cid == chat_id)


async def forge(conversation: Conversation, person: Person, chat_id: int, message_id: int, data: str) -> None:
    """Press with hand-made callback data no button carries (a crafted client)."""
    query_id = f"forged{len(conversation.api.calls)}"
    conversation.api.outstanding_callbacks.add(query_id)
    query = {
        "id": query_id,
        "from": person.payload(),
        "chat_instance": "c",
        "message": conversation.api.message(chat_id, message_id),
        "data": data,
    }
    await conversation.settle(await conversation.push("callback_query", query))


@pytest.fixture
async def keyed(
    conversation: Conversation, settings: Settings
) -> tuple[Conversation, ScriptedModel, FakeManagement]:
    """The conversation with key plumbing faked: management, the plug-in ping, and the key's model."""
    key_model = ScriptedModel(default="key answer")
    management = FakeManagement()
    conversation.settings = conversation.settings.model_copy(update={"keys_secret": "e2e-secret"})
    conversation.overrides |= {
        "management": management,
        "api": FakeKeysAPI(),
        "key_model_factory": key_model.key_factory,
    }
    await conversation.restart()
    conversation.meet(ANA, CREW, status="administrator")
    conversation.meet(BO, CREW)
    hello = await conversation.send(
        ANA, CREW, "hi all"
    )  # Derp knows the group (the link comes from its /settings)
    await conversation.settle(hello)
    return conversation, key_model, management


async def plug_into_crew(conversation: Conversation) -> int:
    """Bo follows the group's plug-in link, pastes the key, picks $100 and the recommended model; Ana turns it on."""
    link = await conversation.send(BO, text=f"/start key-{chat_ref(conversation.settings, CREW.id)}")
    intro = await latest(conversation, "sendRichMessage", BO.id, link.after)
    assert "Plug in your Claude or ChatGPT" in markdown(intro) and "Crew" in markdown(intro)
    await conversation.settle(link)

    pasted = await conversation.send(BO, text=PASTED)
    plugged = await latest(conversation, "sendRichMessage", BO.id, pasted.after)
    await conversation.settle(pasted)
    assert conversation.api.calls_of("deleteMessage", after=pasted.after)  # the key leaves the chat at once
    assert "Plugged in" in markdown(plugged) and PASTED not in markdown(plugged)
    async with conversation.db.session() as session:
        assert (
            await session.scalar(select(StoredMessage).where(StoredMessage.text.contains("sk-proj"))) is None
        )

    before = conversation.seq
    await conversation.press(BO, BO.id, newest(conversation, BO.id), data_of(plugged, "$100"))
    models = await latest(conversation, "editMessageText", BO.id, before)
    assert "$100" in markdown(models) and "GPT-6.1 Sol" in str(models["reply_markup"])

    before = conversation.seq
    await conversation.press(BO, BO.id, int(models["message_id"]), data_of(models, "GPT-6.1 Sol"))
    card = await latest(conversation, "sendRichMessage", CREW.id, before)
    assert "Bo" in markdown(card) and "OpenAI" in markdown(card) and "30 days" in markdown(card)
    asked = await latest(conversation, "editMessageText", BO.id, before)
    assert "Asked the admins" in markdown(asked)

    before = conversation.seq
    card_id = newest(conversation, CREW.id)
    await conversation.press(BO, CREW.id, card_id, data_of(card, "Turn on"))
    assert any(
        "Only admins" in str(c.fields) for c in conversation.api.calls_of("answerCallbackQuery", after=before)
    )
    await conversation.press(ANA, CREW.id, card_id, data_of(card, "Turn on"))
    enabled = await latest(conversation, "editMessageText", CREW.id, before)
    assert "key is on" in markdown(enabled) and "GPT-6.1 Sol" in markdown(enabled)
    async with conversation.db.session() as session:
        key = await session.scalar(select(ContributedKey))
        assert key is not None and key.cap_usd == Decimal(100) and key.model == "openai/gpt-6.1-sol"
        link_row = await session.get(KeyChat, (key.id, CREW.id))
        assert link_row is not None and link_row.state == "on" and link_row.enabled_by == ANA.id
        return key.id


async def test_a_key_plugged_in_privately_answers_the_group_once_an_admin_turns_it_on(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    conversation, key_model, management = keyed
    key_id = await plug_into_crew(conversation)
    assert management.calls == ["create_key", "register"]

    key_model.reply("4, obviously")
    asked = await conversation.send(BO, CREW, "@DerpRobot what's 2+2?", entities=MENTION)
    answer = await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "4, obviously" in markdown(answer)
    assert key_model.sub_keys == ["sk-or-v1-bo"] and key_model.pins[-1].model == "openai/gpt-6.1-sol"
    assert not key_model.pins[-1].zdr and not model.requests  # Derp's own model never ran
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun).order_by(AgentRun.id.desc()))
        calls = (
            list(await session.scalars(select(ProviderCall).where(ProviderCall.run_id == run.id)))
            if run
            else []
        )
    assert run is not None and run.key_id == key_id and run.charged_credits == 0
    assert [(c.funding, c.key_id, c.upstream_usd) for c in calls] == [("key", key_id, Decimal("0.02"))]

    info = await conversation.send(BO, CREW, "/info", reply_to=newest(conversation, CREW.id))
    await conversation.settle(info)
    receipt = next(
        c
        for c in conversation.api.calls_of("sendRichMessage", after=info.after)
        if "About this answer" in markdown(c)
    )
    assert "Bo's ChatGPT key" in markdown(receipt) or "**Bo**" in markdown(receipt)


async def test_a_refusal_is_shown_once_and_the_usual_model_answers_instead(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    conversation, key_model, _ = keyed
    await plug_into_crew(conversation)
    async with conversation.db.transaction() as session:  # the chat can pay its usual model
        await credit(session, (await wallet(session, "chat", CREW.id)).id, "stars:crew", 650, "purchase")

    def refuse(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("I can't help with that.")], finish_reason="content_filter")

    key_model.reply(refuse, refuse)
    model.cost = 0.01  # 12 credits a turn
    model.reply("ha, fine: here's the roast", "second roast")
    asked = await conversation.send(BO, CREW, "@DerpRobot roast Ana", entities=MENTION)
    await conversation.settle(asked)
    replies = said(conversation, CREW.id, asked.after)
    assert "refused this one" in markdown(replies[0]) and "help with that" in markdown(replies[0])
    assert {"Pause here", "Unplug here"} <= {b["text"].split(" ", 1)[1] for b in buttons(replies[0])}
    refusal_id = newest(conversation, CREW.id) - 1  # the usual model's answer came after it
    assert "here's the roast" in markdown(replies[1])
    async with conversation.db.session() as session:
        runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.id)))
    assert [(r.key_id is not None, r.status) for r in runs] == [(True, "failed"), (False, "completed")]
    assert runs[1].charged_credits > 0  # the usual model, on the chat's Derpcoins

    again = await conversation.send(BO, CREW, "@DerpRobot roast Ana again", entities=MENTION)
    await conversation.settle(again)
    later = said(conversation, CREW.id, again.after)
    assert [markdown(c) for c in later] == ["second roast"]  # within the hours a refusal is shown once

    before = conversation.seq
    await conversation.press(ANA, CREW.id, refusal_id, data_of(replies[0], "Unplug here"))
    unplugged = await latest(conversation, "editMessageText", CREW.id, before)
    assert "unplugged from this chat" in markdown(unplugged)
    async with conversation.db.session() as session:
        assert await session.scalar(select(KeyChat.state).where(KeyChat.chat_id == CREW.id)) == "off"


async def test_a_dead_key_is_announced_to_the_chat_and_its_owner_once(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    conversation, key_model, _ = keyed
    key_id = await plug_into_crew(conversation)
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "chat", CREW.id)).id, "stars:crew", 650, "purchase")

    def revoked(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(
            status_code=401,
            model_name="openai/gpt-6.1-sol",
            body={
                "message": "Provider returned error",
                "metadata": {"provider_name": "OpenAI", "is_byok": True},
            },
        )

    key_model.reply(revoked)
    model.reply("usual answer", "usual again")
    asked = await conversation.send(BO, CREW, "@DerpRobot hi", entities=MENTION)
    await conversation.settle(asked)
    group = [markdown(c) for c in said(conversation, CREW.id, asked.after)]
    dms = [markdown(c) for c in said(conversation, BO.id, asked.after)]
    assert any("stopped working" in text for text in group) and group[-1] == "usual answer"
    assert len(dms) == 1 and "stopped working" in dms[0] and "Crew" in dms[0]
    async with conversation.db.session() as session:
        key = await session.get(ContributedKey, key_id)
    assert key is not None and key.dead_at is not None

    again = await conversation.send(BO, CREW, "@DerpRobot hi again", entities=MENTION)
    await conversation.settle(again)
    later = [markdown(c) for c in conversation.api.calls_of("sendRichMessage", after=again.after)]
    assert later == ["usual again"]  # a dead key is skipped and announced only once


async def test_a_key_pasted_into_the_group_is_deleted_and_never_reaches_the_model(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    conversation, _, management = keyed
    pasted = await conversation.send(BO, CREW, f"@DerpRobot use this {PASTED}", entities=MENTION)
    await conversation.settle(pasted)
    warning = await latest(conversation, "sendRichMessage", CREW.id, pasted.after)
    assert "API key" in markdown(warning) and "Plug in privately" in str(warning["reply_markup"])
    assert not model.requests and not management.calls
    async with conversation.db.session() as session:
        assert (
            await session.scalar(select(StoredMessage).where(StoredMessage.text.contains("sk-proj"))) is None
        )


async def test_a_key_the_provider_rejects_is_not_plugged_in(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement],
) -> None:
    conversation, _, management = keyed
    conversation.overrides["api"] = FakeKeysAPI(status=401)
    await conversation.restart()
    pasted = await conversation.send(BO, text=PASTED)
    reply = await latest(conversation, "sendRichMessage", BO.id, pasted.after)
    await conversation.settle(pasted)
    assert "rejected" in markdown(reply)
    assert management.calls == ["create_key", "register", "unregister", "delete_key"]
    async with conversation.db.session() as session:
        assert await session.scalar(select(ContributedKey.id)) is None


async def test_a_forged_turn_on_press_cannot_borrow_someone_elses_key(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement],
) -> None:
    conversation, _, _ = keyed
    key_id = await plug_into_crew(conversation)
    carl, den = Person(1703, "Carl"), Room(-1704, title="Den")
    conversation.meet(carl, den, status="administrator")
    hello = await conversation.send(carl, den, "hi")
    await conversation.settle(hello)
    mine = await conversation.send(
        carl, text="hi derp"
    )  # a message of Derp's in Carl's private chat to press
    await conversation.settle(mine)
    for chat_id in (carl.id, den.id):  # Carl's private chat (where Carl counts as admin) and Carl's group
        await forge(conversation, carl, chat_id, newest(conversation, chat_id), f"key:here_on:{key_id}:")
    async with conversation.db.session() as session:
        borrowed = list(await session.scalars(select(KeyChat).where(KeyChat.chat_id.in_([carl.id, den.id]))))
    assert borrowed == []


async def test_a_key_failing_after_tools_ran_is_not_answered_twice(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    from tests.e2e.model import tool_call

    conversation, key_model, _ = keyed
    key_id = await plug_into_crew(conversation)

    def out_of_credit(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(
            status_code=402,
            model_name="openai/gpt-6.1-sol",
            body={"message": "insufficient_quota", "metadata": {"provider_name": "OpenAI", "is_byok": True}},
        )

    key_model.reply(tool_call("about_derp", topics=["money"]), out_of_credit)
    asked = await conversation.send(BO, CREW, "@DerpRobot how much is a pack?", entities=MENTION)
    await conversation.settle(asked)
    assert not model.requests  # no second run on the usual model: the first had already used a tool
    async with conversation.db.session() as session:
        runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.id)))
        key = await session.get(ContributedKey, key_id)
    assert [r.status for r in runs] == ["failed"]
    assert key is not None and key.paused == "exhausted"  # what it said about the key still counts
    assert [markdown(c) for c in said(conversation, CREW.id, asked.after)] == [
        "Something broke on my side — nothing was charged. Try again in a moment."
    ]


async def test_a_reply_to_a_pasted_key_never_shows_the_key_to_the_model(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    conversation, _, _ = keyed
    conversation.api.failures["deleteMessage"].append(Failure(400, "Bad Request: message can't be deleted"))
    pasted = await conversation.send(ANA, CREW, f"look {PASTED}")
    await conversation.settle(pasted)
    asked = await conversation.send(
        BO, CREW, "@DerpRobot what's this?", entities=MENTION, reply_to=pasted.message_id
    )
    await conversation.settle(asked)
    sent = str(model.requests)
    assert model.requests and "sk-proj" not in sent and "[API key removed]" in sent
    async with conversation.db.session() as session:
        assert (
            await session.scalar(select(StoredMessage).where(StoredMessage.text.contains("sk-proj"))) is None
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_a_chatgpt_key_draws_gifs_and_quick_edits_on_gpt_image_2_6(
    keyed: tuple[Conversation, ScriptedModel, FakeManagement], model: ScriptedModel
) -> None:
    conversation, key_model, _ = keyed
    key_id = await plug_into_crew(conversation)
    api = conversation.overrides["api"]
    assert isinstance(api, FakeKeysAPI)

    key_model.reply(tool_call("generate_image", prompt="a frog dancing on a lily pad", animate=True), ".")
    asked = await conversation.send(BO, CREW, "@DerpRobot make a gif of a dancing frog", entities=MENTION)
    animation = await conversation.expect_reply(asked, method="sendAnimation")
    await conversation.settle(asked)
    [drawn] = api.drawn
    assert drawn["model"] == "openai/gpt-image-2.6-flare" and drawn["api_key"] == "sk-or-v1-bo"
    assert drawn["only"] == ("openai",) and drawn["aspect_ratio"] == "1:1" and drawn["complexity"] is None
    assert "sprite sheet" in drawn["prompt"] and animation.attachments
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun).order_by(AgentRun.id.desc()))
        image = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "image"))
    assert run is not None and run.charged_credits == 0 and not model.requests  # no Derpcoins, no Derp model
    assert image is not None and (image.funding, image.key_id, image.run_id) == ("key", key_id, run.id)
    assert image.upstream_usd == Decimal("0.05")

    conversation.api.register_file(f"chatphoto-{CREW.id}", jpeg("purple"))
    key_model.reply(tool_call("generate_image", prompt="give it a santa hat", chat_photo=True), ".")
    edit = await conversation.send(BO, CREW, "@DerpRobot santa hat on our chat picture", entities=MENTION)
    await conversation.expect_reply(edit, method="sendPhoto")
    await conversation.settle(edit)
    assert api.drawn[-1]["complexity"] == "low" and api.drawn[-1]["aspect_ratio"] == "1:1"  # a quick edit
