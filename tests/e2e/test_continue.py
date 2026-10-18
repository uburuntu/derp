"""Continue after buying: an ask that ran out of Derpcoins picks up once its asker's purchase lands."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.history.models import ChatMember
from derp.money.wallets import balance, wallet
from tests.e2e.fake_api import Call
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel
from tests.e2e.test_billing import paid
from tests.e2e.test_money import exhaust_sponsorship

CARL = Person(1601, "Carl")
DANA = Person(1602, "Dana")
FORUM = Room(-1603, title="Forum", is_forum=True)
PACK = "p1:pack_s_v1:650"  # 50 Stars


def continue_offers(conversation: Conversation, after: int) -> list[Call]:
    return [
        c
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if "Continue your request" in str(c.fields.get("reply_markup"))
    ]


def button_data(call: Call) -> str:
    keyboard: list[list[dict[str, Any]]] = call["reply_markup"]["inline_keyboard"]
    return str(keyboard[0][0]["callback_data"])


async def blocked(conversation: Conversation, settings: Settings, text: str = "what's 2+2?") -> int:
    """Carl asks in private with nobody able to pay: the out-of-Derpcoins offer. Returns the ask's id."""
    await exhaust_sponsorship(conversation, settings)
    asked = await conversation.send(CARL, text=text)
    assert (
        "free Derpcoins are used up" in (await conversation.expect_reply(asked))["rich_message"]["markdown"]
    )
    await conversation.settle(asked)
    return asked.message_id


async def buy(
    conversation: Conversation, person: Person, payload: str, charge: str
) -> tuple[int, list[Call]]:
    sent = await conversation.send(person, successful_payment=paid(payload, 50, charge))
    await conversation.settle(sent)
    return sent.after, continue_offers(conversation, sent.after)


async def test_buying_offers_to_continue_the_ask_and_a_double_tap_runs_it_once(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    ask_id = await blocked(conversation, settings)
    _after, [offer] = await buy(conversation, CARL, f"{PACK}:user:{CARL.id}", "ch-carl")
    assert int(offer["reply_parameters"]["message_id"]) == ask_id and "Derpcoins are in" in str(
        offer["rich_message"]
    )
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)

    model.cost = 0.01  # 12 credits
    model.reply("4, as it happens")
    after = conversation.seq
    await conversation.press(CARL, CARL.id, offer_id, button_data(offer), taps=2)
    answers = [
        c
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if "as it happens" in str(c["rich_message"])
    ]
    assert len(answers) == 1 and int(answers[0]["reply_parameters"]["message_id"]) == ask_id
    assert "reply_markup" not in conversation.api.message(CARL.id, offer_id)  # one press only
    async with conversation.db.session() as session:
        [run] = await session.scalars(select(AgentRun))
        assert (run.trigger_message_id, run.user_id, run.funding, run.charged_credits) == (
            ask_id,
            CARL.id,
            "paid",
            12,
        )
        assert await balance(session, (await wallet(session, "user", CARL.id)).id) == 650 - 12


async def test_a_stale_continue_says_so_and_runs_nothing(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await blocked(conversation, settings)
    _after, [offer] = await buy(conversation, CARL, f"{PACK}:user:{CARL.id}", "ch-stale")
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    async with conversation.db.transaction() as session:
        await session.execute(update(ChatMember).values(waiting_at=datetime.now(UTC) - timedelta(hours=1)))
    await conversation.press(CARL, CARL.id, offer_id, button_data(offer))
    note = conversation.api.message(CARL.id, offer_id)
    assert "no longer waiting" in str(note["rich_message"]) and "reply_markup" not in note
    assert model.requests == []
    async with conversation.db.session() as session:
        assert await session.scalar(select(AgentRun.id)) is None


async def test_an_old_ask_gets_no_offer(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await blocked(conversation, settings)
    async with conversation.db.transaction() as session:
        await session.execute(update(ChatMember).values(waiting_at=datetime.now(UTC) - timedelta(hours=1)))
    _after, offers = await buy(conversation, CARL, f"{PACK}:user:{CARL.id}", "ch-old")
    assert offers == [] and model.requests == []


async def test_a_patron_top_up_never_replays_someone_elses_ask(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await exhaust_sponsorship(conversation, settings)
    conversation.meet(CARL, FORUM)
    conversation.meet(DANA, FORUM)
    conversation.meet(CARL)
    conversation.meet(DANA)  # chat packs are paid by link in the payer's private chat
    asked = await conversation.send(CARL, FORUM, "derp what's 2+2?", thread_id=5)
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    chat_pack = f"p1:pack_s_v1:650:chat:{FORUM.id}:5"

    _after, offers = await buy(
        conversation, DANA, chat_pack, "ch-dana"
    )  # Dana chips in: Carl's ask stays put
    assert offers == [] and model.requests == []
    assert await balance_of(conversation, FORUM.id) == 650

    _after, [offer] = await buy(
        conversation, CARL, chat_pack, "ch-carl"
    )  # Carl's own purchase for that topic
    assert int(offer["chat_id"]) == FORUM.id and int(offer["message_thread_id"]) == 5
    assert int(offer["reply_parameters"]["message_id"]) == asked.message_id
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == FORUM.id)

    after = conversation.seq
    await conversation.press(DANA, FORUM.id, offer_id, button_data(offer))
    refusal = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert "Only the person who asked" in str(refusal["text"]) and model.requests == []

    model.reply("4, on the group")
    after = conversation.seq
    await conversation.press(CARL, FORUM.id, offer_id, button_data(offer))
    [answer] = conversation.api.calls_of("sendRichMessage", after=after)
    assert "on the group" in str(answer["rich_message"]) and int(answer["message_thread_id"]) == 5
    assert int(answer["reply_parameters"]["message_id"]) == asked.message_id
    async with conversation.db.session() as session:
        [run] = await session.scalars(select(AgentRun))
        assert (run.chat_id, run.user_id, run.thread_id, run.funding) == (FORUM.id, CARL.id, 5, "paid")


async def balance_of(conversation: Conversation, chat_id: int) -> int:
    async with conversation.db.session() as session:
        return await balance(session, (await wallet(session, "chat", chat_id)).id)
