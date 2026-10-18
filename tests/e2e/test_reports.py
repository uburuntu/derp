"""Reporting a problem with an answer from its /info receipt, and the owners seeing it in /feedbacks."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from derp.agent.models import AgentRun
from derp.money.wallets import credit, wallet
from derp.ops.models import Feedback
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

ERIN = Person(4101, "Erin")
FINN = Person(4102, "Finn")
OWNER = Person(1000, "Owner")
LOFT = Room(-1004101, title="Loft")  # a private supergroup: its messages have t.me/c links


def keyboard(markup: dict[str, Any] | None) -> dict[str, str]:
    return {b["text"]: b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row}


async def answered(conversation: Conversation, model: ScriptedModel, person: Person, room: Room) -> int:
    """``person`` asks Derp in ``room``; the id of the answer."""
    model.reply("forty-two")
    asked = await conversation.send(
        person, room, "derp, meaning of life?" if room.type != "private" else "hm?"
    )
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    return max(mid for (cid, mid) in conversation.api.messages if cid == room.id)


async def reports(conversation: Conversation) -> list[Feedback]:
    async with conversation.db.session() as session:
        return list(await session.scalars(select(Feedback).order_by(Feedback.id)))


async def test_a_group_member_reports_an_answer_from_the_ephemeral_receipt(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ERIN, LOFT)
    conversation.meet(FINN, LOFT)
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "chat", LOFT.id)).id, "stars:loft", 650, "purchase")
    answer_id = await answered(conversation, model, ERIN, LOFT)

    after = conversation.seq
    await conversation.settle(await conversation.ephemeral_command(ERIN, LOFT, "/info", reply_to=answer_id))
    [receipt] = conversation.api.calls_of("sendRichMessage", after=after)
    assert int(receipt["ephemeral_message_parameters"]["receiver_user_id"]) == ERIN.id
    report = keyboard(receipt["reply_markup"])["⚠️ Report a problem"]
    ephemeral = (LOFT.id, max(eid for (cid, eid) in conversation.api.ephemerals if cid == LOFT.id))
    await conversation.press_ephemeral(ERIN, *ephemeral, report)
    shown = conversation.api.ephemerals[ephemeral]
    assert shown["rich_markdown"] == "What went wrong with this answer?"
    assert list(keyboard(shown["reply_markup"])) == [
        "Wrong answer",
        "Missed context",
        "Wrong image",
        "Broken",
    ]

    missed = keyboard(shown["reply_markup"])["Missed context"]
    await conversation.press_ephemeral(ERIN, *ephemeral, missed, taps=2)  # a double tap files once
    shown = conversation.api.ephemerals[ephemeral]
    assert shown["rich_markdown"].startswith("Reported — thanks.")
    assert list(keyboard(shown["reply_markup"])) == ["Skip"]
    async with conversation.db.session() as session:
        run_id = await session.scalar(select(AgentRun.id))
    [filed] = await reports(conversation)
    assert (filed.kind, filed.run_id, filed.message_id, filed.chat_id) == (
        "missed_context",
        run_id,
        answer_id,
        LOFT.id,
    )
    assert filed.text == ""

    # Someone else's message isn't the details; asking Derp isn't either; Erin's next plain message is.
    await conversation.settle(await conversation.send(FINN, LOFT, "lol"))
    model.reply("still forty-two")
    ask = await conversation.send(ERIN, LOFT, "derp, are you sure?")
    assert (await conversation.expect_reply(ask))["rich_message"]["markdown"] == "still forty-two"
    await conversation.settle(ask)
    assert (await reports(conversation))[0].text == ""
    details = await conversation.send(ERIN, LOFT, "it ignored what Finn said about Tuesday")
    await conversation.settle(details)
    reaction = conversation.api.message(LOFT.id, details.message_id)["_bot_reaction"]
    assert reaction == [{"type": "emoji", "emoji": "👌"}]
    assert not conversation.api.calls_of("sendRichMessage", after=details.after)  # nothing public
    [filed] = await reports(conversation)
    assert filed.text == "it ignored what Finn said about Tuesday" and filed.details_until is None
    later = await conversation.send(ERIN, LOFT, "anyway")
    await conversation.settle(later)
    assert (await reports(conversation))[0].text == "it ignored what Finn said about Tuesday"

    review = await conversation.send(OWNER, text="/feedbacks")
    await conversation.settle(review)
    [page] = conversation.api.calls_of("sendRichMessage", after=review.after)
    text = page["rich_message"]["markdown"]
    assert "missed context" in text and "it ignored what Finn said about Tuesday" in text
    assert f"[run {run_id}](https://t.me/c/4101/{answer_id})" in text


async def report_privately(conversation: Conversation, model: ScriptedModel, category: str) -> int:
    """Erin reports a private answer from its /info receipt under ``category``; the receipt's id."""
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", ERIN.id)).id, "stars:e", 650, "purchase")
    answer_id = await answered(conversation, model, ERIN, Room.private(ERIN))
    info = await conversation.send(ERIN, text="/info", reply_to=answer_id)
    receipt = await conversation.expect_reply(info)
    await conversation.settle(info)
    receipt_id = max(mid for (cid, mid) in conversation.api.messages if cid == ERIN.id)
    await conversation.press(
        ERIN, ERIN.id, receipt_id, keyboard(receipt["reply_markup"])["⚠️ Report a problem"]
    )
    pick = keyboard(conversation.api.message(ERIN.id, receipt_id)["reply_markup"])[category]
    await conversation.press(ERIN, ERIN.id, receipt_id, pick)
    return receipt_id


async def test_a_private_report_can_skip_the_details(
    conversation: Conversation, model: ScriptedModel
) -> None:
    receipt_id = await report_privately(conversation, model, "Wrong answer")
    skip = keyboard(conversation.api.message(ERIN.id, receipt_id)["reply_markup"])["Skip"]
    await conversation.press(ERIN, ERIN.id, receipt_id, skip)
    edit = conversation.api.calls_of("editMessageText")[-1]
    assert edit["rich_message"]["markdown"] == "Reported — thanks."
    assert "reply_markup" not in conversation.api.message(ERIN.id, receipt_id)
    [filed] = await reports(conversation)
    assert (filed.kind, filed.text, filed.details_until) == ("wrong_answer", "", None)

    # Skipped: the next message is an ordinary ask again.
    model.reply("sure")
    asked = await conversation.send(ERIN, text="what about tomorrow?")
    assert (await conversation.expect_reply(asked))["rich_message"]["markdown"] == "sure"
    assert (await reports(conversation))[0].text == ""


async def test_report_details_in_private_get_a_plain_line_until_the_window_closes(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await report_privately(conversation, model, "Broken")
    details = await conversation.send(ERIN, text="the picture never arrived")
    reply = await conversation.expect_reply(details)
    assert reply["rich_message"]["markdown"] == "Added to your report — thanks."
    [filed] = await reports(conversation)
    assert (filed.kind, filed.text) == ("bug", "the picture never arrived")

    # A window that ran out lets the message through to Derp.
    async with conversation.db.transaction() as session:
        await session.execute(
            update(Feedback).values(text="", details_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    model.reply("okay")
    asked = await conversation.send(ERIN, text="hello again")
    assert (await conversation.expect_reply(asked))["rich_message"]["markdown"] == "okay"
    assert (await reports(conversation))[0].text == ""
