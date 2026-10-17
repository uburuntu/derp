from typing import Any

from pydantic_ai.messages import ToolReturnPart
from sqlalchemy import select

from derp.agent.models import AgentRun, RunOutput
from derp.history.models import Chat, Message, User
from derp.money.free import POLICY_VERSION, FreeModels
from derp.money.wallets import credit, wallet
from derp.ops.feedback import FeedbackAction
from derp.ops.models import Feedback
from derp.settings.panel import SettingsAction
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

ANA = Person(2001, "Ana")  # admin
REX = Person(2002, "Rex")
OWNER = Person(1000, "Owner")
DEN = Room(-1021, title="Den")


def result_of(model: ScriptedModel, tool: str) -> str:
    found = [
        str(p.content)
        for m in model.requests[-1]
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == tool
    ]
    return found[-1]


async def ask(conversation: Conversation, model: ScriptedModel, person: Person, *calls: Any) -> int:
    model.reply(*calls, ".")
    asked = await conversation.send(person, DEN, "derp please")
    await conversation.settle(asked)
    return asked.after


async def test_balances_go_by_dm_and_invoices_are_exact(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ANA, DEN, status="administrator")
    conversation.meet(ANA)  # has a private chat with Derp
    conversation.meet(REX, DEN)
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", ANA.id)).id, "stars:ana", 650, "purchase")
        await credit(session, (await wallet(session, "user", REX.id)).id, "stars:rex", 650, "purchase")

    after = await ask(conversation, model, ANA, tool_call("wallet_status"))
    assert "privately" in result_of(model, "wallet_status")
    [dm] = [
        c for c in conversation.api.calls_of("sendRichMessage", after=after) if int(c["chat_id"]) == ANA.id
    ]
    assert "Bought: **" in dm["rich_message"]["markdown"]  # the running answer's hold is already taken out
    assert all(
        "Bought" not in c["rich_message"]["markdown"]
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if int(c["chat_id"]) == DEN.id
    )

    await ask(conversation, model, REX, tool_call("wallet_status"))
    assert "haven't opened a private chat" in result_of(model, "wallet_status")

    after = await ask(conversation, model, REX, tool_call("purchase_button", for_chat=True, stars=50))
    [invoice] = conversation.api.calls_of("sendInvoice", after=after)
    assert int(invoice["chat_id"]) == DEN.id and invoice["currency"] == "XTR"
    assert ":chat:" in invoice["payload"] or str(DEN.id) in invoice["payload"]
    await ask(conversation, model, REX, tool_call("purchase_button", stars=7))
    assert "No pack costs 7⭐" in result_of(model, "purchase_button")

    # A personal invoice asked for in a group goes to the private chat, never into the group.
    after = await ask(conversation, model, ANA, tool_call("purchase_button", stars=50))
    [invoice] = conversation.api.calls_of("sendInvoice", after=after)
    assert int(invoice["chat_id"]) == ANA.id and invoice["payload"] == f"p1:pack_s_v1:650:user:{ANA.id}"
    assert "private chat" in result_of(model, "purchase_button")
    after = await ask(conversation, model, REX, tool_call("purchase_button", stars=50))
    assert "haven't opened a private chat" in result_of(model, "purchase_button")
    # The chat's pack menu uses invoice links: no invoice per pack in the group.
    after = await ask(conversation, model, REX, tool_call("purchase_button", for_chat=True))
    assert not conversation.api.calls_of("sendInvoice", after=after)
    [menu] = [
        c for c in conversation.api.calls_of("sendRichMessage", after=after) if c.fields.get("reply_markup")
    ]
    url = menu["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert conversation.api.invoice_links[url]["payload"] == f"p1:pack_s_v1:650:chat:{DEN.id}"


async def test_an_unconfirmed_invoice_is_not_posted_again(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(REX, DEN)
    conversation.api.fail_next("sendInvoice", error_code=504, description="Gateway Timeout")
    after = await ask(conversation, model, REX, tool_call("purchase_button", stars=50))
    assert len(conversation.api.calls_of("sendInvoice", after=after)) == 1
    result = result_of(model, "purchase_button")
    assert "didn't confirm the invoice" in result and "Don't send it again" in result


async def test_settings_follow_admin_rules_and_context_off_needs_a_press(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ANA, DEN, status="administrator")
    conversation.meet(REX, DEN)
    await ask(conversation, model, REX, tool_call("change_setting", setting="retention_days", value="30"))
    assert result_of(model, "change_setting") == "Only this chat's admins can change that."
    await ask(
        conversation, model, REX, tool_call("change_setting", setting="my_coins_in_groups", value="off")
    )
    assert result_of(model, "change_setting").startswith("Done")
    await ask(conversation, model, ANA, tool_call("change_setting", setting="retention_days", value="30"))
    after = await ask(conversation, model, ANA, tool_call("change_setting", setting="context", value="off"))
    async with conversation.db.session() as session:
        chat = await session.get(Chat, DEN.id)
        assert chat is not None and chat.retention_days == 30 and chat.ambient  # not off until pressed
    [prompt] = [
        c for c in conversation.api.calls_of("sendRichMessage", after=after) if c.fields.get("reply_markup")
    ]
    prompt_id = max(
        mid
        for (cid, mid) in conversation.api.messages
        if cid == DEN.id and conversation.api.messages[(cid, mid)].get("reply_markup")
    )
    purge = SettingsAction(action="ambient", value="purge").pack()
    assert purge in str(prompt["reply_markup"])
    await conversation.press(REX, DEN.id, prompt_id, purge)
    async with conversation.db.session() as session:
        assert (await session.get(Chat, DEN.id)).ambient  # type: ignore[union-attr]
    await conversation.press(ANA, DEN.id, prompt_id, purge)
    async with conversation.db.session() as session:
        assert not (await session.get(Chat, DEN.id)).ambient  # type: ignore[union-attr]


async def test_feedback_reaches_the_owners_review(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(REX, DEN)
    conversation.meet(OWNER)
    await ask(
        conversation, model, REX, tool_call("leave_feedback", text="Quizzes should have timers", kind="idea")
    )
    assert "Filed" in result_of(model, "leave_feedback")
    typed = await conversation.send(REX, DEN, "/feedback the sticker search is great")
    assert "Thanks" in (await conversation.expect_reply(typed))["rich_message"]["markdown"]
    await conversation.settle(typed)
    stranger = await conversation.send(REX, text="/feedbacks")
    await conversation.settle(stranger)
    assert not conversation.api.calls_of("sendRichMessage", after=stranger.after)  # owners only

    review = await conversation.send(OWNER, text="/feedbacks")
    await conversation.settle(review)
    [page] = conversation.api.calls_of("sendRichMessage", after=review.after)
    text = page["rich_message"]["markdown"]
    assert "Open feedback: 2" in text and "Quizzes should have timers" in text and "idea" in text
    async with conversation.db.session() as session:
        first = await session.scalar(select(Feedback.id).order_by(Feedback.id))
    page_id = max(mid for (cid, mid) in conversation.api.messages if cid == OWNER.id)
    await conversation.press(OWNER, OWNER.id, page_id, FeedbackAction(action="done", value=first or 0).pack())
    async with conversation.db.session() as session:
        assert await session.scalar(select(Feedback.state).where(Feedback.id == first)) == "done"


def last_button(conversation: Conversation, chat_id: int, prefix: str = "") -> tuple[int, str]:
    """The newest message in a chat with a button, and its first button's callback data (starting with ``prefix``)."""
    message_id = max(
        mid for (cid, mid), m in conversation.api.messages.items() if cid == chat_id and m.get("reply_markup")
    )
    markup = conversation.api.messages[(chat_id, message_id)]["reply_markup"]
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]
    return message_id, next(data for data in datas if data.startswith(prefix))


async def test_free_models_need_the_persons_own_press(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("change_setting", setting="free_models", value="on"), ".")
    await conversation.settle(await conversation.send(REX, text="turn on free models"))
    assert "on only once they press it themselves" in result_of(model, "change_setting")
    async with conversation.db.session() as session:
        assert (await session.get(User, REX.id)).free_models_consent_version is None  # type: ignore[union-attr]
    notice_id, consent = last_button(conversation, REX.id, "free:consent")
    assert consent == FreeModels(action="consent").pack()
    [notice] = [c for c in conversation.api.calls_of("sendRichMessage") if c.fields.get("reply_markup")]
    assert "may store" in notice["rich_message"]["markdown"] and not notice.fields.get("reply_parameters")
    await conversation.press(REX, REX.id, notice_id, consent)
    async with conversation.db.session() as session:
        assert (await session.get(User, REX.id)).free_models_consent_version == POLICY_VERSION  # type: ignore[union-attr]

    conversation.meet(ANA, DEN, status="administrator")
    conversation.meet(REX, DEN)
    await ask(conversation, model, ANA, tool_call("change_setting", setting="free_models", value="on"))
    assert "an admin presses it" in result_of(model, "change_setting")
    async with conversation.db.session() as session:
        assert not (await session.get(Chat, DEN.id)).free_models  # type: ignore[union-attr]
    notice_id, enable = last_button(conversation, DEN.id, "free:enable")
    await conversation.press(REX, DEN.id, notice_id, enable)
    async with conversation.db.session() as session:
        assert not (await session.get(Chat, DEN.id)).free_models  # type: ignore[union-attr]
    await conversation.press(ANA, DEN.id, notice_id, enable)
    async with conversation.db.session() as session:
        assert (await session.get(Chat, DEN.id)).free_models  # type: ignore[union-attr]


async def test_shorter_retention_from_a_tool_asks_for_a_press(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ANA, DEN, status="administrator")
    shorter = min(conversation.settings.retention.options)
    after = await ask(
        conversation, model, ANA, tool_call("change_setting", setting="retention_days", value=str(shorter))
    )
    assert "Posted a confirmation button" in result_of(model, "change_setting")
    sent = [c["rich_message"]["markdown"] for c in conversation.api.calls_of("sendRichMessage", after=after)]
    assert any(f"older than {shorter} day" in text for text in sent)
    async with conversation.db.session() as session:
        assert (await session.get(Chat, DEN.id)).retention_days is None  # type: ignore[union-attr]
    prompt_id, confirm = last_button(conversation, DEN.id)
    assert confirm == SettingsAction(action="shorten", value=str(shorter)).pack()
    await conversation.press(ANA, DEN.id, prompt_id, confirm)
    async with conversation.db.session() as session:
        assert (await session.get(Chat, DEN.id)).retention_days == shorter  # type: ignore[union-attr]
    longest = max(conversation.settings.retention.options)
    await ask(
        conversation, model, ANA, tool_call("change_setting", setting="retention_days", value=str(longest))
    )
    assert result_of(model, "change_setting").startswith("Done")
    await ask(conversation, model, ANA, tool_call("change_setting", setting="retention_days", value="90"))
    assert "Retention can be" in result_of(model, "change_setting")


async def test_owners_thank_feedback_authors_once(conversation: Conversation) -> None:
    iva = Person(2003, "Iva", language_code="ru")
    conversation.meet(iva)  # started Derp in private: can be thanked
    conversation.meet(iva, DEN)
    conversation.meet(REX, DEN)  # never started Derp in private
    conversation.meet(OWNER)
    for person, words in ((iva, "/feedback больше мемов"), (REX, "/feedback more quizzes")):
        await conversation.settle(await conversation.send(person, DEN, words))
    async with conversation.db.session() as session:
        ids = dict((await session.execute(select(Feedback.user_id, Feedback.id))).all())
    await conversation.settle(await conversation.send(OWNER, text="/feedbacks"))
    page_id = max(mid for (cid, mid) in conversation.api.messages if cid == OWNER.id)
    thank_iva = FeedbackAction(action="thank", value=ids[iva.id]).pack()
    assert thank_iva in str(conversation.api.message(OWNER.id, page_id)["reply_markup"])

    after = conversation.seq
    await conversation.press(OWNER, OWNER.id, page_id, thank_iva, taps=2)  # a double tap thanks once
    dms = [
        c for c in conversation.api.calls_of("sendRichMessage", after=after) if int(c["chat_id"]) == iva.id
    ]
    assert len(dms) == 1 and "Спасибо" in dms[0]["rich_message"]["markdown"]
    first, second = conversation.api.calls_of("answerCallbackQuery", after=after)
    assert "thanked" in first["text"] and "already done" in second["text"] and second["show_alert"]

    after = conversation.seq
    thank_rex = FeedbackAction(action="thank", value=ids[REX.id]).pack()
    await conversation.press(OWNER, OWNER.id, page_id, thank_rex)
    told = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert "couldn't be delivered" in told["text"] and told["show_alert"]
    async with conversation.db.session() as session:
        assert set(await session.scalars(select(Feedback.state))) == {"done"}
    assert "No open feedback" in str(conversation.api.message(OWNER.id, page_id)["rich_message"])


async def test_a_confirmation_card_is_the_answer_so_a_dot_finish_adds_nothing(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ANA, DEN, status="administrator")
    after = await ask(conversation, model, ANA, tool_call("change_setting", setting="context", value="off"))
    *_, card = conversation.api.calls_of("sendRichMessage", after=after)  # (after the join notice)
    assert "ambient:purge" in str(card["reply_markup"])  # the card is the last thing posted: no stray "…"
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.status == "completed"
        outputs = list(await session.scalars(select(RunOutput.kind)))
        stored = await session.scalar(select(Message).where(Message.chat_id == DEN.id, Message.is_derp))
    assert outputs == ["other"] and stored is None  # an output, but UI copy rather than history


async def test_the_free_models_notice_is_the_answer_too(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("change_setting", setting="free_models", value="on"), ".")
    asked = await conversation.send(REX, text="turn on free models")
    await conversation.settle(asked)
    [notice] = conversation.api.calls_of("sendRichMessage", after=asked.after)
    assert "may store" in notice["rich_message"]["markdown"]
    async with conversation.db.session() as session:
        assert list(await session.scalars(select(RunOutput.kind))) == ["other"]
