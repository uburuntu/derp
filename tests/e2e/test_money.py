from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import func, select

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.history.models import Chat, User
from derp.inference.models import ModelPin, ProviderCall
from derp.inference.roles import DEFAULT_PINS, ModelRole
from derp.money.free import POLICY_VERSION
from derp.money.models import Reservation
from derp.money.wallets import balance, capture, credit, reserve, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

CARL = Person(601, "Carl")
OWNER = Person(1000, "Owner")


async def buy(conversation: Conversation, person: Person, amount: int) -> int:
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", person.id)).id
        await credit(session, wallet_id, f"stars:{person.id}", amount, "purchase")
    return wallet_id


async def test_paid_answer_is_charged_its_marked_up_cost(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 500)
    model.cost = 0.01  # ceil(0.01 × 1.2 markup / $0.001) = 12 credits
    model.reply("sure")
    sent = await conversation.send(CARL, text="hi derp")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert model.pins[0].fast  # paid runs use the standard chat pin, routed to fast providers
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.funding, run.charged_credits, run.wallet_id) == (
            "paid",
            12,
            wallet_id,
        )
        assert await balance(session, wallet_id) == 488


async def test_a_paid_answer_without_a_reported_cost_is_charged_an_estimate(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 500)
    async with conversation.db.transaction() as session:
        chat = DEFAULT_PINS[ModelRole.CHAT].model_dump(mode="json")
        prices = {"price_prompt": Decimal(1), "price_completion": Decimal(4)}  # USD per 1M tokens
        session.add(ModelPin(role="chat", pin=chat, **prices))
    model.reply("sure")  # the scripted model reports no cost
    sent = await conversation.send(CARL, text="hi derp")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "chat"))
        run = await session.scalar(select(AgentRun))
        assert call is not None and call.cost_usd > 0
        assert run is not None and run.charged_credits > 0
        assert await balance(session, wallet_id) == 500 - run.charged_credits


async def test_failed_run_releases_the_hold(conversation: Conversation, model: ScriptedModel) -> None:
    wallet_id = await buy(conversation, CARL, 500)

    def boom(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider exploded")

    model.reply(boom)
    sent = await conversation.send(CARL, text="hi derp")
    reply = await conversation.expect_reply(sent)
    assert "nothing was charged" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        assert await balance(session, wallet_id) == 500
        assert await session.scalar(select(Reservation.state)) == "released"


async def test_run_stops_at_the_credits_it_could_hold(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 5)
    model.cost = 0.004  # 5 credits per model turn: the first one uses up the hold
    model.reply(tool_call("search_history", query="cats"), "never reached")
    sent = await conversation.send(CARL, text="find cats")
    reply = await conversation.expect_reply(sent)
    assert "too expensive" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    assert len(model.requests) == 1
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.charged_credits) == ("cancelled", 0)
        assert await balance(session, wallet_id) == 5  # nothing was delivered, so nothing is charged


async def test_a_failure_after_a_reaction_doesnt_claim_nothing_was_charged(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 500)
    model.cost = 0.01  # 12 credits for the turn that reacted

    def boom(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider exploded")

    model.reply(tool_call("react", emoji="🔥"), boom)
    sent = await conversation.send(CARL, text="hi derp")
    reply = (await conversation.expect_reply(sent))["rich_message"]["markdown"]
    assert "work so far was charged" in reply and "weren't charged" not in reply
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.charged_credits) == ("failed", 12)
        assert await balance(session, wallet_id) == 488


async def test_a_capped_run_that_reacted_says_it_was_charged(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 5)
    model.cost = 0.003  # 4 credits a turn: the second one reaches the 5 held
    model.reply(tool_call("react", emoji="🔥"), tool_call("search_history", query="cats"), "never reached")
    sent = await conversation.send(CARL, text="find cats")
    reply = (await conversation.expect_reply(sent))["rich_message"]["markdown"]
    assert "too expensive" in reply and "work so far was charged" in reply
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == 5  # never past the hold
        assert await balance(session, wallet_id) == 0


async def test_no_allowance_left_says_so_without_running(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        session.add(
            ProviderCall(
                kind="chat",
                model="m",
                funding="sponsored",
                cost_usd=Decimal(str(settings.sponsored_global_daily_usd)),
            )
        )
    sent = await conversation.send(CARL, text="hi derp")
    reply = await conversation.expect_reply(sent)
    assert "free Derpcoins are used up" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    assert model.requests == []
    async with conversation.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0
    pings = [c for c in conversation.api.calls if c.method == "sendMessage" and int(c["chat_id"]) == OWNER.id]
    assert len(pings) == 1 and "cap" in str(pings[0]["text"])


async def test_a_run_that_dies_outside_the_model_is_closed_and_released(
    conversation: Conversation, model: ScriptedModel
) -> None:
    wallet_id = await buy(conversation, CARL, 500)

    def broken(_pin: object) -> object:
        raise RuntimeError("factory down")

    conversation.overrides["model_factory"] = broken
    await conversation.restart()
    sent = await conversation.send(CARL, text="hi derp")
    assert "Something broke" in (await conversation.expect_reply(sent))["rich_message"]["markdown"]
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.error_type) == ("failed", "RuntimeError")
        assert await session.scalar(select(Reservation.state)) == "released"
        assert await balance(session, wallet_id) == 500


async def exhaust_sponsorship(conversation: Conversation, settings: Settings) -> None:
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        cap = Decimal(str(settings.sponsored_global_daily_usd))
        session.add(ProviderCall(kind="chat", model="m", funding="sponsored", cost_usd=cap))


async def press_button(
    conversation: Conversation, person: Person, chat_id: int, call: Any, row: int = 0, column: int = 0
) -> None:
    data = call["reply_markup"]["inline_keyboard"][row][column]["callback_data"]
    message_id = max(mid for (cid, mid) in conversation.api.messages if cid == chat_id)
    await conversation.press(person, chat_id, message_id, data)


async def test_consenting_to_free_models_answers_the_waiting_question(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    asked = await conversation.send(CARL, text="what's 2+2?")
    offer = await conversation.expect_reply(asked)
    text = offer["rich_message"]["markdown"]
    assert "may store" in text and "](tg://time?unix=" in text  # the refill time, in each reader's zone
    [[buy, consent]] = offer["reply_markup"]["inline_keyboard"]
    assert (buy["text"], consent["text"]) == ("⭐ Buy Derpcoins", "🆓 Use free models")
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    after_buy = conversation.seq
    await conversation.press(CARL, CARL.id, offer_id, buy["callback_data"])
    [menu] = conversation.api.calls_of("editMessageText", after=after_buy)  # in place, no new message
    assert "Pick an option:" in menu["rich_message"]["markdown"]
    assert consent["callback_data"] in str(menu["reply_markup"])
    model.reply("4, on the house")
    after = conversation.seq
    await conversation.press(CARL, CARL.id, offer_id, consent["callback_data"])
    answers = [
        c
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if "on the house" in c["rich_message"]["markdown"]
    ]
    assert answers and int(answers[0]["reply_parameters"]["message_id"]) == asked.message_id
    assert model.pins[-1].model.endswith(":free")
    assert not model.infos[-1].model_request_parameters.native_tools  # search fees would land on the creator
    async with conversation.db.session() as session:
        user = await session.get(User, CARL.id)
        assert user is not None and user.free_models_consent_version is not None
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.funding, run.charged_credits) == ("free", 0)


async def test_only_an_admin_turns_free_models_on_for_a_group(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    den = Room(-1013, title="Den")
    boss = Person(602, "Boss")
    conversation.meet(CARL, den)
    conversation.meet(boss, den, status="administrator")
    asked = await conversation.send(CARL, den, "derp hi")
    offer = await conversation.expect_reply(asked)
    assert offer["reply_markup"]["inline_keyboard"][0][0]["text"] == "⭐ Top up this chat"
    await press_button(conversation, CARL, den.id, offer, row=1)  # not an admin
    async with conversation.db.session() as session:
        chat = await session.get(Chat, den.id)
        assert chat is not None and not chat.free_models
    model.reply("free hello")
    after = conversation.seq
    await press_button(conversation, boss, den.id, offer, row=1)
    texts = [c["rich_message"]["markdown"] for c in conversation.api.calls_of("sendRichMessage", after=after)]
    assert any("allowed free models" in text for text in texts) and "free hello" in texts
    async with conversation.db.session() as session:
        chat = await session.get(Chat, den.id)
        assert chat is not None and chat.free_models


async def test_busy_free_models_say_so(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    async with conversation.db.transaction() as session:
        session.add(User(id=CARL.id, first_name="Carl", free_models_consent_version=POLICY_VERSION))

    def busy(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=429, model_name="free")

    model.reply(busy, busy)  # the retry on the rotated free chain is busy too
    asked = await conversation.send(CARL, text="hi")
    assert "free models are busy" in (await conversation.expect_reply(asked))["rich_message"]["markdown"]


async def test_a_failure_before_the_model_runs_returns_the_hold(
    conversation: Conversation, model: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from derp.agent import runtime

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("window")

    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", CARL.id)).id, "stars:c", 650, "purchase")
    monkeypatch.setattr(runtime, "build_window", broken)
    sent = await conversation.send(CARL, text="hi derp")
    assert "nothing was charged" in (await conversation.expect_reply(sent))["rich_message"]["markdown"]
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        assert await session.scalar(select(Reservation.state)) == "released"
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.error_type, run.charged_credits) == (
            "failed",
            "RuntimeError",
            0,
        )


async def test_free_model_runs_keep_the_long_tail_in_the_toolbox(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    async with conversation.db.transaction() as session:
        session.add(User(id=CARL.id, first_name="Carl", free_models_consent_version=POLICY_VERSION))
    model.reply("hi")
    asked = await conversation.send(CARL, text="hi")
    await conversation.expect_reply(asked)
    names = {tool.name for tool in model.infos[-1].function_tools}
    assert "search_tools" in names and "search_history" in names and "inspect_media" not in names
    assert "speak" not in names  # free models never get voice messages


async def test_a_group_offer_comes_once_a_day_per_person_then_a_quiet_reaction(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    den = Room(-1015, title="Den")
    dana = Person(603, "Dana")
    conversation.meet(CARL, den)
    conversation.meet(dana, den)
    first = await conversation.send(CARL, den, "derp hi")
    offer = await conversation.expect_reply(first)
    assert "](tg://time?unix=" in offer["rich_message"]["markdown"]
    top_up = offer["reply_markup"]["inline_keyboard"][0][0]
    assert top_up["text"] == "⭐ Top up this chat"

    again = await conversation.send(CARL, den, "derp hello??")
    await conversation.settle(again)
    assert not conversation.api.calls_of("sendRichMessage", after=again.after)
    [reaction] = conversation.api.calls_of("setMessageReaction", after=again.after)
    assert int(reaction["message_id"]) == again.message_id
    assert reaction["reaction"] == [{"type": "emoji", "emoji": "🙈"}]
    other = await conversation.send(dana, den, "derp yo")  # each person gets their own offer
    assert (
        "free Derpcoins are used up" in (await conversation.expect_reply(other))["rich_message"]["markdown"]
    )

    offer_id = max(
        mid for (cid, mid), m in conversation.api.messages.items() if cid == den.id and m.get("reply_markup")
    )
    after = conversation.seq
    await conversation.press(CARL, den.id, offer_id, top_up["callback_data"])
    [menu] = conversation.api.calls_of("sendRichMessage", after=after)
    assert int(menu["ephemeral_message_parameters"]["receiver_user_id"]) == CARL.id
    assert "Chat balance" in menu["rich_message"]["markdown"]
    assert not conversation.api.calls_of("editMessageText", after=after)  # the offer stays for others
    async with conversation.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_an_emptied_chat_wallet_says_so_once_per_depletion(
    conversation: Conversation, model: ScriptedModel
) -> None:
    loft = Room(-1016, title="Loft")
    conversation.meet(OWNER)
    conversation.meet(CARL, loft)
    async with conversation.db.transaction() as session:
        chat_wallet = (await wallet(session, "chat", loft.id)).id
        await credit(session, chat_wallet, "grant:loft", 24, "grant")
    model.cost = 0.01  # 12 credits an answer

    async def ask(text: str) -> list[Any]:
        model.reply("ok")
        sent = await conversation.send(CARL, loft, text)
        await conversation.settle(sent)
        return [
            c
            for c in conversation.api.calls_of("sendRichMessage", after=sent.after)
            if "Chat wallet's empty" in c["rich_message"]["markdown"]
        ]

    assert await ask("derp one") == []  # 12 left: still pays a turn
    [line] = await ask("derp two")
    assert line["disable_notification"] in (True, "true") and not line.fields.get("reply_parameters")
    assert line["reply_markup"]["inline_keyboard"][0][0]["text"] == "⭐ Top up this chat"
    assert await ask("derp three") == []  # the free daily allowance pays now; no repeat

    # A top-up re-arms it: the next time the wallet runs dry, it's said again.
    payload = f"p1:pack_s_v1:650:chat:{loft.id}"
    payment = {
        "currency": "XTR", "total_amount": 50, "invoice_payload": payload,
        "telegram_payment_charge_id": "ch-loft", "provider_payment_charge_id": "",
    }  # fmt: skip
    await conversation.settle(await conversation.send(CARL, loft, successful_payment=payment))
    async with conversation.db.transaction() as session:
        await reserve(session, chat_wallet, "turn:elsewhere", 638)
        await capture(session, "turn:elsewhere", 638)
    assert len(await ask("derp four")) == 1
    async with conversation.db.session() as session:
        assert await balance(session, chat_wallet) == 0
