import asyncio

from derp.agent.lanes import Lanes


async def test_same_topic_runs_queue_and_rapid_fire_merges_into_the_newest() -> None:
    lanes = Lanes(concurrency=4)
    order: list[str] = []
    release = asyncio.Event()

    async def run(user: int, message_id: int, *, hold: bool = False) -> bool:
        async with lanes.turn((1, None), user, message_id) as go:
            if go:
                order.append(f"start {message_id}")
                if hold:
                    await release.wait()
                order.append(f"end {message_id}")
            return go

    first = asyncio.create_task(run(7, 10, hold=True))
    await asyncio.sleep(0)
    others = [
        asyncio.create_task(run(7, 11)),
        asyncio.create_task(run(8, 12)),
        asyncio.create_task(run(7, 13)),
    ]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, *others)
    assert results == [True, False, True, True]  # #11 is superseded by the same user's #13
    assert order == ["start 10", "end 10", "start 12", "end 12", "start 13", "end 13"]
    assert lanes.busy == 0


async def test_topics_run_side_by_side_within_the_global_cap() -> None:
    lanes = Lanes(concurrency=1)
    inside = 0
    peak = 0

    async def run(key: tuple[int, int | None], message_id: int) -> None:
        nonlocal inside, peak
        async with lanes.turn(key, 1, message_id) as go:
            assert go
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(run((1, None), 1), run((1, 5), 2), run((2, None), 3))
    assert peak == 1


async def test_scheduled_prompts_are_never_merged_away() -> None:
    lanes = Lanes(concurrency=4)
    release = asyncio.Event()
    ran: list[int] = []

    async def run(message_id: int, *, hold: bool = False) -> bool:
        async with lanes.turn((1, None), 7, message_id) as go:
            if go:
                ran.append(message_id)
                if hold:
                    await release.wait()
            return go

    busy = asyncio.create_task(run(10, hold=True))
    await asyncio.sleep(0)
    job = asyncio.create_task(run(0))  # a scheduled prompt: id 0, never stored
    await asyncio.sleep(0)
    newer = asyncio.create_task(run(11))  # the creator speaks while it waits
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(busy, job, newer) == [True, True, True]
    assert ran == [10, 0, 11]
