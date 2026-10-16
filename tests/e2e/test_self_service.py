from typing import Any

from pydantic_ai.messages import ToolReturnPart
from sqlalchemy import select

from derp.history.models import Chat
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
