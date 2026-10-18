"""Continue after buying: an ask that ran out of Derpcoins picks up once its asker's purchase lands."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic_ai.messages import BinaryContent
from sqlalchemy import select, update

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.history.models import ChatMember
from derp.history.models import Message as StoredMessage
from derp.money.wallets import balance, wallet
from tests.e2e.fake_api import Call
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel
from tests.e2e.test_billing import paid
from tests.e2e.test_effects import jpeg
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
    assert int(offer["reply_parameters"]["message_id"]) == ask_id and "ran out of Derpcoins" in str(
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
    toast = conversation.api.calls_of("answerCallbackQuery", after=after)[0]
    assert toast.sequence < answers[0].sequence  # the button stops spinning before the run
    async with conversation.db.session() as session:
        [run] = await session.scalars(select(AgentRun))
        assert (run.trigger_message_id, run.user_id, run.funding, run.charged_credits) == (
            ask_id,
            CARL.id,
            "paid",
            12,
        )
        assert await balance(session, (await wallet(session, "user", CARL.id)).id) == 650 - 12


async def test_a_continued_ask_still_sees_what_it_replied_to(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    """Telegram doesn't nest replies in the pressed message: the reply comes back from the stored ask."""
    await exhaust_sponsorship(conversation, settings)
    conversation.api.register_file("cat", jpeg("orange"))
    photo = [{"file_id": "cat", "file_unique_id": "u-cat", "width": 64, "height": 64}]
    posted = await conversation.send(CARL, photo=photo)
    await conversation.settle(posted)
    async with conversation.db.transaction() as session:  # long ago: only the reply ties it to the question
        await session.execute(
            update(StoredMessage)
            .where(StoredMessage.message_id == posted.message_id)
            .values(date=datetime.now(UTC) - timedelta(hours=2))
        )
    asked = await conversation.send(CARL, text="what breed is this?", reply_to=posted.message_id)
    await conversation.settle(asked)
    _after, [offer] = await buy(conversation, CARL, f"{PACK}:user:{CARL.id}", "ch-reply")
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    model.reply("a ginger one")
    await conversation.press(CARL, CARL.id, offer_id, button_data(offer))
    run_request = next(r for r in model.requests if "what breed" in str(r[-1].parts))
    assert any(isinstance(item, BinaryContent) for part in run_request[-1].parts for item in part.content)  # type: ignore[union-attr]


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


async def test_continue_on_a_deleted_question_says_it_is_stale(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await blocked(conversation, settings)
    _after, [offer] = await buy(conversation, CARL, f"{PACK}:user:{CARL.id}", "ch-gone")
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    conversation.api.messages[(CARL.id, offer_id)].pop("reply_to_message")  # gone: Telegram sends no reply
    await conversation.press(CARL, CARL.id, offer_id, button_data(offer))
    assert "no longer waiting" in str(conversation.api.message(CARL.id, offer_id)["rich_message"])
    assert model.requests == []


async def test_consenting_after_the_question_went_stale_says_so(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await blocked(conversation, settings)
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    async with conversation.db.transaction() as session:
        await session.execute(update(ChatMember).values(waiting_at=datetime.now(UTC) - timedelta(hours=1)))
    consent = conversation.api.message(CARL.id, offer_id)["reply_markup"]["inline_keyboard"][0][1]
    await conversation.press(CARL, CARL.id, offer_id, consent["callback_data"])
    assert "no longer waiting" in str(conversation.api.message(CARL.id, offer_id)["rich_message"])
    assert model.requests == []


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


async def test_buying_for_yourself_offers_to_continue_your_ask_in_a_group(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    """Personal packs are paid in private, but your own Derpcoins pay in the group too: the button goes there."""
    await exhaust_sponsorship(conversation, settings)
    conversation.meet(CARL, FORUM)
    conversation.meet(CARL)
    asked = await conversation.send(CARL, FORUM, "derp what's 2+2?", thread_id=5)
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    _after, [offer] = await buy(conversation, CARL, f"{PACK}:user:{CARL.id}", "ch-own")
    assert int(offer["chat_id"]) == FORUM.id and int(offer["message_thread_id"]) == 5
    assert int(offer["reply_parameters"]["message_id"]) == asked.message_id
    assert "650" not in str(offer["rich_message"])  # nothing about the purchase in the group
    offer_id = max(mid for (cid, mid) in conversation.api.messages if cid == FORUM.id)
    model.reply("4, on me")
    after = conversation.seq
    await conversation.press(CARL, FORUM.id, offer_id, button_data(offer))
    [answer] = conversation.api.calls_of("sendRichMessage", after=after)
    assert "on me" in str(answer["rich_message"]) and int(answer["message_thread_id"]) == 5
    async with conversation.db.session() as session:
        [run] = await session.scalars(select(AgentRun))
        assert run.wallet_id == (await wallet(session, "user", CARL.id)).id
