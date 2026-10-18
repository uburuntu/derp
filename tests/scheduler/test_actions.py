import asyncio
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from derp.db.engine import Database
from derp.history.models import Chat
from derp.scheduler.jobs import CHAT_LIMIT, Change, Snooze, act_on_job
from derp.scheduler.models import Job
from derp.scheduler.worker import claim_due

pytestmark = pytest.mark.db
GROUP = -100
CREATOR, OTHER = 1, 2


async def setup(
    db: Database, repeat: str | None = None, *, due: timedelta = timedelta(hours=1), kind: str = "message"
) -> Chat:
    """A group with one active job of CREATOR's, due after ``due``; returns the chat."""
    at = datetime.now(UTC) + due
    async with db.transaction() as session:
        chat = Chat(id=GROUP, type="supergroup", timezone="Europe/Berlin")
        session.add(chat)
        session.add(Job(chat_id=GROUP, creator_id=CREATOR, kind=kind, text="bins", repeat=repeat,
                        next_at=at, anchor_at=at))  # fmt: skip
    return chat


async def job_of(db: Database) -> Job:
    async with db.session() as session:
        job = await session.scalar(select(Job).order_by(Job.id))
    assert job is not None
    return job


async def act(db: Database, chat: Chat, user: int, admin: bool, action: Change | Snooze | str) -> Job | str:
    async with db.transaction() as session:
        return await act_on_job(session, chat, 1, user, admin, action)  # type: ignore[arg-type]


async def test_creators_and_admins_move_and_reword_a_job_others_cannot(database: Database) -> None:
    chat = await setup(database)
    later = datetime.now(UTC) + timedelta(days=1)
    assert await act(database, chat, OTHER, False, Change(at=later)) == "not_yours"
    moved = await act(database, chat, CREATOR, False, Change(at=later, text="recycling"))
    assert isinstance(moved, Job) and moved.next_at == later == moved.anchor_at and moved.text == "recycling"
    reworded = await act(database, chat, OTHER, True, Change(text="glass"))  # a plain reminder: any admin
    assert isinstance(reworded, Job) and reworded.text == "glass" and reworded.next_at == later


async def test_a_job_that_will_repeat_needs_an_admin_and_once_stops_it(database: Database) -> None:
    chat = await setup(database, "daily")
    assert await act(database, chat, CREATOR, False, Change(text="water")) == "admins"
    weekly = await act(database, chat, CREATOR, True, Change(repeat="weekly"))
    assert isinstance(weekly, Job) and weekly.repeat == "weekly"
    once = await act(database, chat, CREATOR, False, Change(repeat="once"))  # no admin needed to stop
    assert isinstance(once, Job) and once.repeat is None
    assert await act(database, chat, CREATOR, False, Change(repeat="daily")) == "admins"


async def test_a_fired_one_off_comes_back_only_with_a_new_time(database: Database) -> None:
    chat = await setup(database, due=-timedelta(seconds=1))
    assert len(await claim_due(database, datetime.now(UTC))) == 1
    assert (await job_of(database)).state == "done"
    assert await act(database, chat, CREATOR, False, Change(text="again")) == "passed"
    assert await act(database, chat, CREATOR, False, "cancel") == "gone"
    later = datetime.now(UTC) + timedelta(hours=2)
    back = await act(database, chat, CREATOR, False, Change(at=later))
    assert isinstance(back, Job) and back.state == "active" and back.next_at == later


async def test_a_fired_one_off_stays_finished_when_the_chat_is_full(database: Database) -> None:
    chat = await setup(database, due=-timedelta(seconds=1))
    await claim_due(database, datetime.now(UTC))
    at = datetime.now(UTC) + timedelta(hours=1)
    async with database.transaction() as session:
        session.add_all(Job(chat_id=GROUP, creator_id=OTHER + n, kind="message", text="x", next_at=at, anchor_at=at)
                        for n in range(CHAT_LIMIT))  # fmt: skip
    assert await act(database, chat, CREATOR, False, Change(at=at)) == "full"


async def test_a_change_racing_a_firing_neither_doubles_nor_loses_it(database: Database) -> None:
    chat = await setup(database, due=-timedelta(seconds=1))
    later = datetime.now(UTC) + timedelta(days=1)
    async with database.transaction() as session:  # an edit holds the row as the worker ticks
        await act_on_job(session, chat, 1, CREATOR, False, Change(at=later))
        assert await claim_due(database, datetime.now(UTC)) == []
    assert await claim_due(database, datetime.now(UTC)) == []  # it moved: not due any more
    assert (await job_of(database)).next_at == later

    async with database.transaction() as session:  # due again; this time the worker claims first
        (await session.get(Job, 1, with_for_update=True)).next_at = datetime.now(UTC)  # type: ignore[union-attr]
    assert await behind_a_claim(database, chat, Change(text="late edit")) == "passed"  # fired once


async def behind_a_claim(db: Database, chat: Chat, action: Change | Snooze) -> Job | str:
    """CREATOR's ``action`` on job 1 while the worker holds its claim on it, as a one-off firing."""
    claimed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def worker() -> None:
        async with db.transaction() as session:
            job = await session.get(Job, 1, with_for_update=True)
            assert job is not None
            job.state, job.runs = "done", job.runs + 1  # what claim_due does to a one-off
            claimed.set_result(None)
            await release.wait()

    ticking = asyncio.create_task(worker())
    await claimed
    acting = asyncio.create_task(act(db, chat, CREATOR, False, action))
    await asyncio.sleep(0.2)
    assert not acting.done()  # waits for the claim instead of changing what is firing
    release.set()
    await ticking
    return await acting


async def test_snoozing_a_fired_one_off_moves_it_and_a_stale_button_refuses(database: Database) -> None:
    chat = await setup(database, due=-timedelta(seconds=1))
    [(fired, _)] = await claim_due(database, datetime.now(UTC))
    soon = datetime.now(UTC) + timedelta(minutes=10)
    assert await act(database, chat, OTHER, False, Snooze(soon, fired.runs)) == "not_yours"
    snoozed = await act(database, chat, CREATOR, False, Snooze(soon, fired.runs))
    assert (
        isinstance(snoozed, Job) and snoozed.id == 1 and snoozed.state == "active" and snoozed.next_at == soon
    )
    again = await act(
        database, chat, OTHER, True, Snooze(soon + timedelta(hours=1), fired.runs)
    )  # a double tap
    assert isinstance(again, Job) and again.next_at == soon + timedelta(hours=1)
    assert len(await claim_due(database, soon + timedelta(hours=1))) == 1  # fires once more
    assert await act(database, chat, CREATOR, False, Snooze(soon, fired.runs)) == "fired"


async def test_a_snooze_pressed_as_it_fires_again_is_stale(database: Database) -> None:
    chat = await setup(database, due=-timedelta(seconds=1))
    await claim_due(database, datetime.now(UTC))
    assert isinstance(await act(database, chat, CREATOR, False, Snooze(datetime.now(UTC), 1)), Job)
    assert await behind_a_claim(database, chat, Snooze(datetime.now(UTC) + timedelta(hours=1), 1)) == "fired"


async def test_snoozing_a_repeating_reminder_adds_one_extra_firing(database: Database) -> None:
    chat = await setup(database, "daily", due=-timedelta(seconds=1))
    await claim_due(database, datetime.now(UTC))
    series = await job_of(database)
    soon = datetime.now(UTC) + timedelta(minutes=10)
    extra = await act(database, chat, CREATOR, False, Snooze(soon, 1))  # a one-off: no admin needed
    assert isinstance(extra, Job) and extra.snooze_of == 1 and extra.repeat is None and extra.next_at == soon
    later = soon + timedelta(hours=1)
    moved = await act(database, chat, CREATOR, False, Snooze(later, 1))
    assert isinstance(moved, Job) and moved.id == extra.id and moved.next_at == later
    async with database.session() as session:
        jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
    assert [(j.id, j.repeat, j.next_at) for j in jobs] == [
        (1, "daily", series.next_at),
        (extra.id, None, later),
    ]
    assert await act(database, chat, CREATOR, True, "cancel") != "gone"
    async with database.session() as session:
        assert set((await session.scalars(select(Job.state))).all()) == {"cancelled"}


async def test_what_a_prompt_costs_its_creator_stays_the_creators(database: Database) -> None:
    chat = await setup(database, "daily", kind="prompt")
    for change in (
        Change(text="roast us"),
        Change(kind="message"),
        Change(repeat="weekly"),
        Change(repeat="once"),
    ):
        assert await act(database, chat, OTHER, True, change) == "creators"
    later = datetime.now(UTC) + timedelta(days=1)
    moved = await act(database, chat, OTHER, True, Change(at=later, text="bins"))  # same text: only a move
    assert isinstance(moved, Job) and moved.next_at == later
    assert await act(database, chat, OTHER, False, "pause") == "not_yours"
    for action in ("pause", "resume", "cancel"):
        assert isinstance(await act(database, chat, OTHER, True, action), Job)


async def test_a_reminder_becomes_a_prompt_only_by_its_creator(database: Database) -> None:
    chat = await setup(database)
    assert await act(database, chat, OTHER, True, Change(kind="prompt")) == "creators"
    made = await act(database, chat, CREATOR, False, Change(kind="prompt", text="tell a joke"))
    assert isinstance(made, Job) and (made.kind, made.text) == ("prompt", "tell a joke")


async def test_a_new_repeat_alone_moves_the_next_run_onto_the_rule(database: Database) -> None:
    berlin = ZoneInfo("Europe/Berlin")
    today = datetime.now(berlin).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)  # a Saturday ahead
    at = datetime.combine(saturday, time(9), berlin).astimezone(UTC)
    chat = await setup(database, "weekly")
    async with database.transaction() as session:
        await session.execute(update(Job).values(next_at=at, anchor_at=at))
    weekdays = await act(database, chat, CREATOR, True, Change(repeat="weekdays"))
    assert isinstance(weekdays, Job)
    assert (
        weekdays.next_at.astimezone(berlin).weekday() == 0 and weekdays.next_at.astimezone(berlin).hour == 9
    )
    assert weekdays.anchor_at == at  # the series keeps its wall time and day


async def test_pausing_a_series_pauses_its_snoozed_extra_and_resuming_brings_both_back(
    database: Database,
) -> None:
    chat = await setup(database, "daily", due=-timedelta(seconds=1))
    await claim_due(database, datetime.now(UTC))
    soon = datetime.now(UTC) + timedelta(minutes=10)
    extra = await act(database, chat, CREATOR, False, Snooze(soon, 1))
    assert isinstance(extra, Job)

    async def states() -> list[str]:
        async with database.session() as session:
            return list((await session.scalars(select(Job.state).order_by(Job.id))).all())

    await act(database, chat, CREATOR, False, "pause")
    assert await states() == ["paused", "paused"]
    await act(database, chat, CREATOR, False, "resume")
    assert await states() == ["active", "active"]
    await act(database, chat, CREATOR, False, "pause")
    async with database.transaction() as session:  # the extra's moment passes while paused
        await session.execute(update(Job).where(Job.id == extra.id).values(next_at=datetime.now(UTC)))
    await act(database, chat, CREATOR, False, "resume")
    assert await states() == ["active", "cancelled"]
