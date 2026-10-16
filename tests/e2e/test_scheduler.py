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
