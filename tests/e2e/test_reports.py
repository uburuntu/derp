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


async def open_report(conversation: Conversation, person: Person, chat_id: int, receipt_id: int) -> str:
    """Open Report a problem on a receipt Derp sent in ``chat_id``; the callback data of "Wrong answer"."""
    report = keyboard(conversation.api.message(chat_id, receipt_id)["reply_markup"])["⚠️ Report a problem"]
    await conversation.press(person, chat_id, receipt_id, report)
    return keyboard(conversation.api.message(chat_id, receipt_id)["reply_markup"])["Wrong answer"]


async def test_details_come_only_from_the_chat_where_the_receipt_was_pressed(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ERIN, LOFT)
    conversation.meet(ERIN)
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "chat", LOFT.id)).id, "stars:loft", 650, "purchase")
        await credit(session, (await wallet(session, "user", ERIN.id)).id, "stars:e", 650, "purchase")
    answer_id = await answered(conversation, model, ERIN, LOFT)
    await conversation.settle(await conversation.ephemeral_command(ERIN, LOFT, "/info", reply_to=answer_id))
    ephemeral = (LOFT.id, max(eid for (cid, eid) in conversation.api.ephemerals if cid == LOFT.id))
    shown = conversation.api.ephemerals[ephemeral]
    await conversation.press_ephemeral(
        ERIN, *ephemeral, keyboard(shown["reply_markup"])["⚠️ Report a problem"]
    )
    pick = keyboard(conversation.api.ephemerals[ephemeral]["reply_markup"])["Wrong answer"]
    await conversation.press_ephemeral(ERIN, *ephemeral, pick)
    assert "everyone in the chat will see it" in conversation.api.ephemerals[ephemeral]["rich_markdown"]

    # A DM to Derp inside the window is an ordinary ask, not the group report's text.
    model.reply("hi in private")
    dm = await conversation.send(ERIN, text="hey, unrelated")
    assert (await conversation.expect_reply(dm))["rich_message"]["markdown"] == "hi in private"
    await conversation.settle(dm)
    assert (await reports(conversation))[0].text == ""
    details = await conversation.send(ERIN, LOFT, "it got the date wrong")
    await conversation.settle(details)
    assert conversation.api.message(LOFT.id, details.message_id)["_bot_reaction"] == [
        {"type": "emoji", "emoji": "👌"}
    ]
    assert (await reports(conversation))[0].text == "it got the date wrong"


async def test_a_receipt_sent_by_dm_takes_the_details_in_private_not_in_the_group(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ERIN, LOFT)
    conversation.meet(ERIN)
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "chat", LOFT.id)).id, "stars:loft", 650, "purchase")
    answer_id = await answered(conversation, model, ERIN, LOFT)
    info = await conversation.send(ERIN, LOFT, "/info", reply_to=answer_id)  # sent the ordinary way: by DM
    await conversation.settle(info)
    receipt_id = max(mid for (cid, mid) in conversation.api.messages if cid == ERIN.id)
    await conversation.press(
        ERIN, ERIN.id, receipt_id, await open_report(conversation, ERIN, ERIN.id, receipt_id)
    )
    [filed] = await reports(conversation)
    assert (filed.chat_id, filed.details_chat_id) == (LOFT.id, ERIN.id)

    # Erin's next group message is just a message: nothing marks it, nothing reveals the report.
    chatter = await conversation.send(ERIN, LOFT, "anyone up for lunch?")
    await conversation.settle(chatter)
    assert "_bot_reaction" not in conversation.api.message(LOFT.id, chatter.message_id)
    assert (await reports(conversation))[0].text == ""
    details = await conversation.send(ERIN, text="the answer was about the wrong Tuesday")
    reply = await conversation.expect_reply(details)
    assert reply["rich_message"]["markdown"] == "Added to your report — thanks."
    assert (await reports(conversation))[0].text == "the answer was about the wrong Tuesday"


async def test_one_details_window_at_a_time_and_a_filled_report_keeps_its_text(
    conversation: Conversation, model: ScriptedModel
) -> None:
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", ERIN.id)).id, "stars:e", 650, "purchase")

    async def receipt(answer_id: int) -> int:
        await conversation.settle(await conversation.send(ERIN, text="/info", reply_to=answer_id))
        return max(mid for (cid, mid) in conversation.api.messages if cid == ERIN.id)

    answers = [await answered(conversation, model, ERIN, Room.private(ERIN)) for _ in range(2)]
    first, second = [await receipt(answer_id) for answer_id in answers]
    await conversation.press(ERIN, ERIN.id, first, await open_report(conversation, ERIN, ERIN.id, first))
    wrong = await open_report(conversation, ERIN, ERIN.id, second)
    await conversation.press(ERIN, ERIN.id, second, wrong)

    details = await conversation.send(ERIN, text="the second one was off")
    assert (await conversation.expect_reply(details))["rich_message"]["markdown"].startswith("Added")
    model.reply("an ordinary answer")
    asked = await conversation.send(ERIN, text="and what about Friday?")  # the first report's window closed
    assert (await conversation.expect_reply(asked))["rich_message"]["markdown"] == "an ordinary answer"
    await conversation.settle(asked)
    assert [r.text for r in await reports(conversation)] == ["", "the second one was off"]

    # Recategorizing the filled report keeps its text and opens no window.
    again = await receipt(answers[1])
    wrong = await open_report(conversation, ERIN, ERIN.id, again)
    await conversation.press(ERIN, ERIN.id, again, wrong.replace("wrong_answer", "wrong_image"))
    assert (
        conversation.api.calls_of("editMessageText")[-1]["rich_message"]["markdown"] == "Reported — thanks."
    )
    model.reply("still ordinary")
    asked = await conversation.send(ERIN, text="ok then")
    assert (await conversation.expect_reply(asked))["rich_message"]["markdown"] == "still ordinary"
    await conversation.settle(asked)
    filled = (await reports(conversation))[1]
    assert (filled.kind, filled.text, filled.details_until) == ("wrong_image", "the second one was off", None)
