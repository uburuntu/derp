"""Run scheduling: one run at a time per chat topic, a global cap, and merged rapid-fire triggers.

Runs in the same chat topic queue behind each other, so each sees the previous
answer in its window. If someone sends several invoking messages while they
wait, only the newest is answered — the earlier ones are already in its
context — and one that arrives after a newer one of theirs already ran there
(it was held up in capture: screened, transcribed) is not answered at all. A
global semaphore bounds concurrent runs against the database and the provider.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

LaneKey = tuple[int, int | None]  # (chat, forum topic)
RAN_KEPT = 10_000  # (topic, user) pairs remembered; forgetting one only lets a late older trigger run


@dataclass(slots=True)
class _Lane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    newest: dict[int, int] = field(default_factory=dict[int, int])  # user → newest waiting trigger id
    users: int = 0


class Lanes:
    def __init__(self, concurrency: int) -> None:
        self._lanes: dict[LaneKey, _Lane] = {}
        self._slots = asyncio.Semaphore(concurrency)
        self._ran: dict[tuple[LaneKey, int], int] = {}  # (topic, user) → newest trigger id that ran

    @asynccontextmanager
    async def turn(
        self, key: LaneKey, user_id: int, message_id: int, *, ordered: bool = True
    ) -> AsyncGenerator[bool]:
        """Yields True when this trigger should run, False when a newer one from the same user supersedes it
        or, ``ordered``, already ran in this topic.

        Synthetic triggers (id 0, scheduled prompts) are never stored, so no newer message contains them:
        they always run and never supersede anyone.
        """
        lane = self._lanes.setdefault(key, _Lane())
        lane.users += 1
        if message_id > 0:
            lane.newest[user_id] = max(lane.newest.get(user_id, 0), message_id)
        try:
            async with lane.lock:
                ran = self._ran.get((key, user_id), 0) if ordered else 0
                if message_id > 0 and max(lane.newest.get(user_id, 0), ran) > message_id:
                    yield False
                    return
                if message_id > 0:
                    lane.newest.pop(user_id, None)
                    if len(self._ran) >= RAN_KEPT:
                        self._ran.clear()
                    self._ran[(key, user_id)] = max(self._ran.get((key, user_id), 0), message_id)
                async with self._slots:
                    yield True
        finally:
            lane.users -= 1
            if lane.users == 0:
                self._lanes.pop(key, None)

    @property
    def busy(self) -> int:
        return len(self._lanes)
