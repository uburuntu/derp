from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from derp.history.models import Chat
from derp.scheduler.models import Job
from derp.scheduler.worker import claim_due
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_self_service import result_of

LEE = Person(2101, "Lee")
ADA = Person(2102, "Ada")  # admin
MOE = Person(2103, "Moe")
PORCH = Room(-2110, title="Porch")
MOSCOW = ZoneInfo("Europe/Moscow")


def nine_tomorrow(tz: ZoneInfo) -> tuple[str, datetime]:
    day = datetime.now(tz).date() + timedelta(days=1)
    return f"{day} 09:00", datetime.combine(day, time(9), tz).astimezone(UTC)


async def test_a_local_time_waits_for_the_zone_then_fires_at_local_nine(
    conversation: Conversation, model: ScriptedModel
) -> None:
    at, nine = nine_tomorrow(MOSCOW)
    model.reply(tool_call("schedule", text="call mum", at=at, repeat="daily"), "Where are you?")
    asked = await conversation.send(LEE, text="remind me every day at 9 to call mum")
    await conversation.settle(asked)
    assert "time zone isn't set" in str(model.requests[0][-1])  # the situation says so
    assert "Ask once where they are" in result_of(model, "schedule")
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.id)) is None  # no silent UTC guess

    model.reply(
        tool_call("change_setting", setting="time_zone", value="europe/moscow"),
        tool_call("schedule", text="call mum", at=at, repeat="daily"),
        "Done.",
    )
    await conversation.settle(await conversation.send(LEE, text="I'm in Moscow"))
    assert "Europe/Moscow" in result_of(model, "change_setting")
    assert result_of(model, "schedule").startswith("Scheduled #")
    async with conversation.db.session() as session:
        chat = await session.get(Chat, LEE.id)
        job = await session.scalar(select(Job))
    assert chat is not None and chat.timezone == "Europe/Moscow"
    assert job is not None and job.next_at == nine and nine.time() == time(6)  # 09:00 MSK is 06:00 UTC

    await claim_due(conversation.db, nine)  # it fires and moves on to the next local 09:00
    async with conversation.db.session() as session:
        job = await session.scalar(select(Job))
    assert job is not None and job.next_at == nine + timedelta(days=1)


async def test_anyone_sets_a_missing_zone_admins_change_it_and_repeats_follow(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ADA, PORCH, status="administrator")
    conversation.meet(MOE, PORCH)
    model.reply(tool_call("change_setting", setting="time_zone", value="Europe/Berlin"), ".")
    await conversation.settle(await conversation.send(MOE, PORCH, "derp we're in Berlin"))
    assert result_of(model, "change_setting").startswith("Done")

    berlin = ZoneInfo("Europe/Berlin")
    at, nine = nine_tomorrow(berlin)
    model.reply(tool_call("schedule", text="standup", at=at, repeat="weekdays"), ".")
    await conversation.settle(await conversation.send(ADA, PORCH, "derp standup at 9 on weekdays"))
    model.reply(tool_call("schedule", text="pizza", in_minutes=90), ".")
    await conversation.settle(await conversation.send(MOE, PORCH, "derp pizza in 90 min"))
    async with conversation.db.session() as session:
        pizza_at = await session.scalar(select(Job.next_at).where(Job.text == "pizza"))

    model.reply(tool_call("change_setting", setting="time_zone", value="Europe/Moscow"), ".")
    await conversation.settle(await conversation.send(MOE, PORCH, "derp actually Moscow"))
    assert result_of(model, "change_setting") == "Only this chat's admins can change that."
    model.reply(tool_call("change_setting", setting="time_zone", value="Mars/Olympus"), ".")
    await conversation.settle(await conversation.send(ADA, PORCH, "derp we live on Mars"))
    assert "unknown time zone" in result_of(model, "change_setting")
    model.reply(tool_call("change_setting", setting="time_zone", value="Europe/Moscow"), ".")
    await conversation.settle(await conversation.send(ADA, PORCH, "derp we moved to Moscow"))
    async with conversation.db.session() as session:
        jobs = {job.text: job for job in await session.scalars(select(Job))}
        chat = await session.get(Chat, PORCH.id)
    assert chat is not None and chat.timezone == "Europe/Moscow"
    assert jobs["pizza"].next_at == pizza_at  # a one-off keeps its instant
    standup = jobs["standup"].next_at.astimezone(MOSCOW)
    assert standup.time() == time(9) and standup.date() == nine.astimezone(berlin).date()
