"""Live Telegram actor-role resolution with a small bounded cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import TYPE_CHECKING, Final

from aiogram.enums import ChatMemberStatus, ChatType

from derp.observability import report_exception
from derp.tools.policy import ActorRole

if TYPE_CHECKING:
    from aiogram import Bot

DEFAULT_ACTOR_ROLE_CACHE_TTL: Final = timedelta(seconds=30)
MAX_ACTOR_ROLE_CACHE_TTL: Final = timedelta(minutes=5)
DEFAULT_ACTOR_ROLE_CACHE_ENTRIES: Final = 1024
MAX_ACTOR_ROLE_CACHE_ENTRIES: Final = 10_000

type CacheKey = tuple[int, int]
type Clock = Callable[[], float]

_ADMIN_STATUSES = frozenset(
    {
        ChatMemberStatus.CREATOR.value,
        ChatMemberStatus.ADMINISTRATOR.value,
    }
)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    role: ActorRole
    expires_at: float


class ActorRoleResolver:
    """Resolve Telegram actor roles with bounded TTL and single-flight lookups."""

    def __init__(
        self,
        bot: Bot,
        *,
        cache_ttl: timedelta = DEFAULT_ACTOR_ROLE_CACHE_TTL,
        max_cache_entries: int = DEFAULT_ACTOR_ROLE_CACHE_ENTRIES,
        clock: Clock = monotonic,
    ) -> None:
        if not timedelta(0) < cache_ttl <= MAX_ACTOR_ROLE_CACHE_TTL:
            raise ValueError(
                "actor-role cache TTL must be between zero and five minutes"
            )
        if (
            isinstance(max_cache_entries, bool)
            or not 1 <= max_cache_entries <= MAX_ACTOR_ROLE_CACHE_ENTRIES
        ):
            raise ValueError("actor-role cache entries must be between 1 and 10000")

        self._bot = bot
        self._cache_ttl_seconds = cache_ttl.total_seconds()
        self._max_cache_entries = max_cache_entries
        self._clock = clock
        self._cache: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()
        self._inflight: dict[CacheKey, asyncio.Task[ActorRole]] = {}
        self._lock = asyncio.Lock()

    async def resolve(
        self,
        *,
        chat_id: int,
        chat_type: str,
        user_id: int,
    ) -> ActorRole:
        """Return the least privileged live role supported by Telegram state."""
        if chat_type == ChatType.PRIVATE.value:
            return ActorRole.PRIVATE_OWNER

        key = (chat_id, user_id)
        async with self._lock:
            now = self._clock()
            self._purge_expired(now)
            if entry := self._cache.get(key):
                self._cache.move_to_end(key)
                return entry.role

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._lookup_and_cache(key),
                    name=f"actor-role:{chat_id}:{user_id}",
                )
                self._inflight[key] = task

        return await asyncio.shield(task)

    async def invalidate(self, *, chat_id: int, user_id: int) -> None:
        """Invalidate one actor so the next resolve performs a live lookup."""
        key = (chat_id, user_id)
        async with self._lock:
            self._cache.pop(key, None)
            self._inflight.pop(key, None)

    async def _lookup_and_cache(self, key: CacheKey) -> ActorRole:
        task = asyncio.current_task()
        try:
            role = await self._lookup_role(*key)
        except BaseException:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
            raise

        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
                self._store(key, role)
        return role

    async def _lookup_role(self, chat_id: int, user_id: int) -> ActorRole:
        try:
            member = await self._bot.get_chat_member(chat_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report_exception(
                "actor_role_lookup_failed",
                exception=exc,
                level="warning",
                **{
                    "telegram.chat_id": chat_id,
                    "telegram.user_id": user_id,
                },
            )
            return ActorRole.MEMBER

        status = member.status
        if isinstance(status, ChatMemberStatus):
            status = status.value
        return ActorRole.ADMIN if status in _ADMIN_STATUSES else ActorRole.MEMBER

    def _purge_expired(self, now: float) -> None:
        for key, entry in tuple(self._cache.items()):
            if entry.expires_at <= now:
                del self._cache[key]

    def _store(self, key: CacheKey, role: ActorRole) -> None:
        self._cache[key] = _CacheEntry(
            role=role,
            expires_at=self._clock() + self._cache_ttl_seconds,
        )
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)


__all__ = [
    "ActorRoleResolver",
    "DEFAULT_ACTOR_ROLE_CACHE_ENTRIES",
    "DEFAULT_ACTOR_ROLE_CACHE_TTL",
    "MAX_ACTOR_ROLE_CACHE_ENTRIES",
    "MAX_ACTOR_ROLE_CACHE_TTL",
]
