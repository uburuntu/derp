from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update

from derp.inference.models import ProviderCall
from derp.money.wallets import claw_back, credit, wallet
from derp.scheduler.models import Job
from derp.scheduler.worker import run_due
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
