from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update

from derp.inference.models import ProviderCall
from derp.money.wallets import claw_back, credit, wallet
from derp.scheduler.jobs import MISS_LIMIT, ResumeJob, SnoozeJob
from derp.scheduler.models import Job
from derp.scheduler.worker import claim_due, run_due
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

LEE = Person(1601, "Lee")
MAX = Person(1602, "Max")
YARD = Room(-1016, title="Yard")
OWNER = Person(1000, "Owner")


async def fire_now(conversation: Conversation) -> int:
    async with conversation.db.transaction() as session:
        await session.execute(update(Job).values(next_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert conversation.services is not None and conversation.dispatcher is not None
    return await run_due(conversation.db, conversation.services.bot, conversation.dispatcher["runtime"])


async def test_a_reminder_fires_once_in_its_chat(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(LEE, YARD)
    model.reply(tool_call("schedule", text="take the bins out", in_minutes=30), "Will do.")
    asked = await conversation.send(LEE, YARD, "derp remind us in 30 min to take the bins out")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "tg://time?unix=" in str(model.requests[-1][-1])  # the tool result shows a viewer-local time
    after = conversation.seq
    assert await fire_now(conversation) == 1
    posted = conversation.api.calls_of("sendRichMessage", after=after)
    assert [c["rich_message"]["markdown"] for c in posted] == ["⏰ Reminder from Lee: take the bins out"]
    assert int(posted[0]["chat_id"]) == YARD.id
    assert await fire_now(conversation) == 0  # done: never again
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.state)) == "done"


async def test_a_reminder_moves_in_place_and_fires_at_the_new_time(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LEE, YARD)
    conversation.meet(MAX, YARD)
    model.reply(tool_call("schedule", text="take the bins out", in_minutes=30), "Will do.")
    await conversation.settle(
        await conversation.send(LEE, YARD, "derp remind us in 30 min to take the bins out")
    )
    async with conversation.db.session() as session:
        job_id = await session.scalar(select(Job.id))
    model.reply(tool_call("edit_job", job_id=job_id, in_minutes=5), "Nope.")
    await conversation.settle(
        await conversation.send(MAX, YARD, "derp move Lee's reminder to 5 min from now")
    )
    assert "Only its creator or an admin" in str(model.requests[-1][-1])

    model.reply(tool_call("edit_job", job_id=job_id, in_minutes=120, text="bins and recycling"), "Moved.")
    await conversation.settle(
        await conversation.send(LEE, YARD, "derp move it to two hours from now, add recycling")
    )
    assert f"Updated job {job_id}: next run ![" in str(model.requests[-1][-1])
    async with conversation.db.session() as session:
        [job] = (await session.scalars(select(Job))).all()  # the same job, changed in place
    assert job.id == job_id and job.text == "bins and recycling"
    assert job.next_at > datetime.now(UTC) + timedelta(minutes=110)
    assert (
        await claim_due(conversation.db, datetime.now(UTC) + timedelta(minutes=31)) == []
    )  # not at the old time
    after = conversation.seq
    assert await fire_now(conversation) == 1
    posted = conversation.api.calls_of("sendRichMessage", after=after)
    assert [c["rich_message"]["markdown"] for c in posted] == ["⏰ Reminder from Lee: bins and recycling"]


def last_reminder(conversation: Conversation, chat_id: int) -> tuple[int, list[str]]:
    """The newest message with buttons in a chat and its buttons' callback data."""
    message_id, message = max(
        (mid, m)
        for (cid, mid), m in conversation.api.messages.items()
        if cid == chat_id and m.get("reply_markup")
    )
    return message_id, [b["callback_data"] for row in message["reply_markup"]["inline_keyboard"] for b in row]


async def test_a_fired_reminder_snoozes_for_its_creator_and_fires_again(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LEE, YARD)
    conversation.meet(MAX, YARD)
    model.reply(tool_call("schedule", text="stretch", in_minutes=30), "Ok.")
    await conversation.settle(await conversation.send(LEE, YARD, "derp remind me to stretch"))
    await fire_now(conversation)
    reminder, buttons = last_reminder(conversation, YARD.id)
    async with conversation.db.session() as session:
        job_id = await session.scalar(select(Job.id))
    assert job_id is not None
    assert buttons == [SnoozeJob(id=job_id, run=1, by=by).pack() for by in ("10m", "1h", "1d")]
    shown = conversation.api.message(YARD.id, reminder)["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in shown[0]] == ["+10 min", "+1 h", "Tomorrow"]

    after = conversation.seq
    await conversation.press(MAX, YARD.id, reminder, buttons[0])
    refusal = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert "Only its creator or an admin" in refusal["text"] and refusal["show_alert"]

    after = conversation.seq
    await conversation.press(LEE, YARD.id, reminder, buttons[0], taps=2)  # a double tap snoozes once
    toasts = [c["text"] for c in conversation.api.calls_of("answerCallbackQuery", after=after)]
    assert toasts == ["💤 Snoozed: back in 10 minutes."] * 2
    assert "reply_markup" not in conversation.api.message(YARD.id, reminder)
    async with conversation.db.session() as session:
        [job] = (await session.scalars(select(Job))).all()
    left = job.next_at - datetime.now(UTC)
    assert job.state == "active" and timedelta(minutes=9) < left <= timedelta(minutes=10)
    assert await claim_due(conversation.db, datetime.now(UTC)) == []  # not due yet

    after = conversation.seq
    assert await fire_now(conversation) == 1
    posted = conversation.api.calls_of("sendRichMessage", after=after)
    assert [c["rich_message"]["markdown"] for c in posted] == ["⏰ Reminder from Lee: stretch"]
    assert last_reminder(conversation, YARD.id)[1][0] == SnoozeJob(id=job_id, run=2, by="10m").pack()


async def test_snoozing_a_daily_reminder_adds_a_firing_and_keeps_the_series(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("schedule", text="vitamins", in_minutes=30, repeat="daily"), "Ok.")
    await conversation.settle(await conversation.send(LEE, text="remind me daily about vitamins"))
    await fire_now(conversation)
    async with conversation.db.session() as session:
        series = await session.scalar(select(Job))
    assert series is not None
    reminder, buttons = last_reminder(conversation, LEE.id)
    after = conversation.seq
    await conversation.press(LEE, LEE.id, reminder, buttons[2])
    toast = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert toast["text"] == "💤 Snoozed until this time tomorrow."
    async with conversation.db.session() as session:
        jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
    assert [(j.repeat, j.next_at, j.snooze_of) for j in jobs[:1]] == [("daily", series.next_at, None)]
    assert jobs[1].repeat is None and jobs[1].snooze_of == series.id
    assert jobs[1].next_at - datetime.now(UTC) > timedelta(hours=23)
    after = conversation.seq
    assert await fire_now(conversation) == 2  # the series and its extra firing
    posted = conversation.api.calls_of("sendRichMessage", after=after)
    assert [c["rich_message"]["markdown"] for c in posted] == ["⏰ Reminder from Lee: vitamins"] * 2


async def test_repeating_jobs_need_an_admin_in_groups(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(MAX, YARD)
    model.reply(tool_call("schedule", text="standup", in_minutes=60, repeat="daily"), "Nope.")
    asked = await conversation.send(MAX, YARD, "derp remind us daily about standup")
    await conversation.settle(asked)
    assert "Only admins" in str(model.requests[-1][-1])
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.id)) is None


async def test_a_scheduled_prompt_is_answered_and_paused_when_unfunded(
    conversation: Conversation, model: ScriptedModel
) -> None:
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", LEE.id)).id, "stars:lee", 650, "purchase")
    model.reply(
        tool_call("schedule", text="tell me a joke", in_minutes=5, repeat="daily", answer_it=True), "Set."
    )
    asked = await conversation.send(LEE, text="every day tell me a joke")
    await conversation.settle(asked)
    model.reply("why did the derp cross the road")
    after = conversation.seq
    await fire_now(conversation)
    jokes = conversation.api.calls_of("sendRichMessage", after=after)
    assert jokes and jokes[-1]["rich_message"]["markdown"] == "why did the derp cross the road"
    assert "scheduled task" in str(model.requests[-1][-1])

    async with conversation.db.transaction() as session:  # Derpcoins refunded, sponsorship exhausted
        await claw_back(session, "stars:lee", "refund:lee")
        session.add(ProviderCall(kind="chat", model="m", funding="sponsored", cost_usd=Decimal(1000)))
    conversation.meet(OWNER)  # the sponsorship cap pings the owner
    for _ in range(3):
        await fire_now(conversation)
    async with conversation.db.session() as session:
        job = await session.scalar(select(Job))
        assert job is not None and job.state == "paused" and job.misses == 3
    notes = [c["rich_message"]["markdown"] for c in conversation.api.calls_of("sendRichMessage", after=after)]
    assert any("Skipped scheduled job" in n for n in notes) and any(
        "Paused scheduled job" in n for n in notes
    )


async def test_a_chat_that_refuses_derp_pauses_its_jobs(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LEE, YARD, status="administrator")
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", LEE.id)).id, "stars:lee2", 650, "purchase")
    model.reply(
        tool_call("schedule", text="daily fact", in_minutes=5, repeat="daily", answer_it=True), "Set."
    )
    asked = await conversation.send(LEE, YARD, "derp every day tell us a fact")
    await conversation.settle(asked)
    model.reply("cats sleep a lot")
    conversation.api.fail_next("sendRichMessage", error_code=403, description="Forbidden: bot was kicked")
    await fire_now(conversation)
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.state)) == "paused"


async def test_removing_derp_or_leaving_pauses_jobs(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(LEE, YARD, status="administrator")
    conversation.meet(MAX, YARD)
    model.reply(tool_call("schedule", text="stretch", in_minutes=30), "Ok.")
    await conversation.settle(await conversation.send(MAX, YARD, "derp remind me to stretch"))
    model.reply(tool_call("schedule", text="water", in_minutes=30, repeat="daily"), "Ok.")
    await conversation.settle(await conversation.send(LEE, YARD, "derp daily water reminder"))
    left = {"message_id": 900, "date": 1_800_000_000, "chat": YARD.payload(MAX), "from": MAX.payload(),
            "left_chat_member": MAX.payload()}  # fmt: skip
    conversation.api.register_message(left)
    await conversation.settle(await conversation.push("message", left))
    async with conversation.db.session() as session:
        states = dict((await session.execute(select(Job.creator_id, Job.state))).all())
    assert states == {MAX.id: "paused", LEE.id: "active"}
    kicked = {
        "chat": YARD.payload(),
        "from": LEE.payload(),
        "date": 0,
        "old_chat_member": {"status": "member", "user": conversation.api.bot_user},
        "new_chat_member": {"status": "kicked", "user": conversation.api.bot_user, "until_date": 0},
    }
    await conversation.settle(await conversation.push("my_chat_member", kicked))
    async with conversation.db.session() as session:
        assert set((await session.scalars(select(Job.state))).all()) == {"paused"}


async def test_a_paused_job_resumes_from_its_note_for_its_creator_only(conversation: Conversation) -> None:
    conversation.meet(LEE, YARD)
    conversation.meet(MAX, YARD)
    conversation.meet(OWNER)  # the sponsorship cap pings the owner
    await conversation.settle(await conversation.send(LEE, YARD, "hi"))
    now = datetime.now(UTC)
    async with conversation.db.transaction() as session:
        job = Job(chat_id=YARD.id, creator_id=LEE.id, kind="prompt", text="tell a joke", repeat="daily",
                  next_at=now, anchor_at=now, misses=MISS_LIMIT - 1)  # fmt: skip
        session.add(job)
        session.add(ProviderCall(kind="chat", model="m", funding="sponsored", cost_usd=Decimal(1000)))
        await session.flush()
        job_id = job.id
    await fire_now(conversation)
    note_id, note = max(
        (mid, m)
        for (cid, mid), m in conversation.api.messages.items()
        if cid == YARD.id and m.get("reply_markup")
    )
    assert "Paused scheduled job" in str(note["rich_message"])
    [[resume]] = note["reply_markup"]["inline_keyboard"]
    assert resume["text"] == "▶️ Resume" and resume["callback_data"] == ResumeJob(id=job_id).pack()

    after = conversation.seq
    await conversation.press(MAX, YARD.id, note_id, resume["callback_data"])
    refusal = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert "Only its creator or an admin" in refusal["text"] and refusal["show_alert"]
    assert conversation.api.message(YARD.id, note_id).get("reply_markup")  # still there for Lee

    await conversation.press(LEE, YARD.id, note_id, resume["callback_data"])
    async with conversation.db.session() as session:
        resumed = await session.get(Job, job_id)
        assert resumed is not None and resumed.state == "active" and resumed.misses == 0
    shown = conversation.api.message(YARD.id, note_id)
    assert "reply_markup" not in shown and "is back on" in str(shown["rich_message"])


async def test_a_scheduled_prompt_stays_its_creators_and_a_firing_never_edits_it(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LEE, YARD, status="administrator")
    conversation.meet(MAX, YARD, status="administrator")
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", LEE.id)).id, "stars:lee", 650, "purchase")
    model.reply(
        tool_call("schedule", text="tell us a joke", in_minutes=30, repeat="daily", answer_it=True), "Set."
    )
    await conversation.settle(await conversation.send(LEE, YARD, "derp every day tell us a joke"))
    async with conversation.db.session() as session:
        job_id = await session.scalar(select(Job.id))

    model.reply(tool_call("edit_job", job_id=job_id, text="write us an essay", repeat="weekly"), "Nope.")
    await conversation.settle(await conversation.send(MAX, YARD, "derp make Lee's joke a weekly essay"))
    assert "only they can make one, reword it" in str(model.requests[-1][-1])  # Lee's Derpcoins, Lee's call
    model.reply(tool_call("edit_job", job_id=job_id), "Hm.")
    await conversation.settle(await conversation.send(LEE, YARD, "derp change the joke job"))
    assert "Nothing to change" in str(model.requests[-1][-1])

    model.reply(tool_call("edit_job", job_id=job_id, repeat="once"), "here's a joke")
    await fire_now(conversation)
    assert "scheduled task firing now" in str(model.requests[-1][-1])
    async with conversation.db.session() as session:
        job = await session.get(Job, job_id)
        assert job is not None and (job.text, job.repeat) == ("tell us a joke", "daily")
