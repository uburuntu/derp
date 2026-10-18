import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from derp.db.engine import Database
from derp.history.models import Chat
from derp.scheduler.jobs import CHAT_LIMIT, Change, act_on_job
from derp.scheduler.models import Job
from derp.scheduler.worker import claim_due

pytestmark = pytest.mark.db
GROUP = -100
CREATOR, OTHER = 1, 2


async def setup(db: Database, repeat: str | None = None, *, due: timedelta = timedelta(hours=1)) -> Chat:
    """A group with one active job of CREATOR's, due after ``due``; returns the chat."""
    at = datetime.now(UTC) + due
    async with db.transaction() as session:
        chat = Chat(id=GROUP, type="supergroup", timezone="Europe/Berlin")
        session.add(chat)
        session.add(Job(chat_id=GROUP, creator_id=CREATOR, kind="message", text="bins", repeat=repeat,
                        next_at=at, anchor_at=at))  # fmt: skip
    return chat


async def job_of(db: Database) -> Job:
    async with db.session() as session:
        job = await session.scalar(select(Job).order_by(Job.id))
    assert job is not None
    return job


async def act(db: Database, chat: Chat, user: int, admin: bool, action: Change | str) -> Job | str:
    async with db.transaction() as session:
        return await act_on_job(session, chat, 1, user, admin, action)  # type: ignore[arg-type]


async def test_creators_and_admins_move_and_reword_a_job_others_cannot(database: Database) -> None:
    chat = await setup(database)
    later = datetime.now(UTC) + timedelta(days=1)
    assert await act(database, chat, OTHER, False, Change(at=later)) == "not_yours"
    moved = await act(database, chat, CREATOR, False, Change(at=later, text="recycling"))
    assert isinstance(moved, Job) and moved.next_at == later == moved.anchor_at and moved.text == "recycling"
    reworded = await act(database, chat, OTHER, True, Change(kind="prompt"))
    assert isinstance(reworded, Job) and reworded.kind == "prompt" and reworded.next_at == later


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
    claimed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def worker() -> None:
        async with database.transaction() as session:
            job = await session.get(Job, 1, with_for_update=True)
            assert job is not None
            job.state, job.runs = "done", job.runs + 1  # what claim_due does to a one-off
            claimed.set_result(None)
            await release.wait()

    ticking = asyncio.create_task(worker())
    await claimed
    edit = asyncio.create_task(act(database, chat, CREATOR, False, Change(text="late edit")))
    await asyncio.sleep(0.2)
    assert not edit.done()  # waits for the claim instead of editing what is firing
    release.set()
    await ticking
    assert await edit == "passed"  # it fired once; the edit saw the finished row
