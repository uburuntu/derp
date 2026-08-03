"""Tests for live Telegram actor-role resolution."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from derp.tools.authorization import ActorRoleResolver
from derp.tools.policy import ActorRole


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def member(status: str) -> SimpleNamespace:
    return SimpleNamespace(status=status)


async def test_private_sender_is_owner_without_telegram_lookup() -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock())
    resolver = ActorRoleResolver(bot)

    role = await resolver.resolve(chat_id=42, chat_type="private", user_id=42)

    assert role is ActorRole.PRIVATE_OWNER
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.parametrize("status", ["creator", "administrator"])
async def test_group_owner_and_administrator_resolve_as_admin(status: str) -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member(status)))
    resolver = ActorRoleResolver(bot)

    role = await resolver.resolve(chat_id=-100, chat_type="supergroup", user_id=42)

    assert role is ActorRole.ADMIN


@pytest.mark.parametrize(
    "status",
    ["member", "restricted", "left", "kicked", "unknown"],
)
async def test_every_non_admin_group_status_resolves_as_member(status: str) -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member(status)))
    resolver = ActorRoleResolver(bot)

    role = await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)

    assert role is ActorRole.MEMBER


async def test_reuses_cached_role_until_ttl_expires() -> None:
    clock = MutableClock()
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            side_effect=[member("member"), member("administrator")]
        )
    )
    resolver = ActorRoleResolver(
        bot,
        cache_ttl=timedelta(seconds=1),
        clock=clock,
    )

    assert (
        await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        is ActorRole.MEMBER
    )
    clock.now = 0.9
    assert (
        await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        is ActorRole.MEMBER
    )
    clock.now = 1.1
    assert (
        await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        is ActorRole.ADMIN
    )
    assert bot.get_chat_member.await_count == 2


async def test_cache_is_bounded_and_evicts_least_recent_actor() -> None:
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=member("member")),
    )
    resolver = ActorRoleResolver(bot, max_cache_entries=2)

    await resolver.resolve(chat_id=-100, chat_type="group", user_id=1)
    await resolver.resolve(chat_id=-100, chat_type="group", user_id=2)
    await resolver.resolve(chat_id=-100, chat_type="group", user_id=1)
    await resolver.resolve(chat_id=-100, chat_type="group", user_id=3)
    await resolver.resolve(chat_id=-100, chat_type="group", user_id=2)

    assert bot.get_chat_member.await_count == 4


async def test_concurrent_lookups_for_same_actor_are_single_flight() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def lookup(chat_id: int, user_id: int) -> SimpleNamespace:
        started.set()
        await release.wait()
        return member("administrator")

    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
    resolver = ActorRoleResolver(bot)
    resolutions = [
        asyncio.create_task(
            resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        )
        for _ in range(5)
    ]

    await started.wait()
    assert bot.get_chat_member.await_count == 1
    release.set()

    assert await asyncio.gather(*resolutions) == [ActorRole.ADMIN] * 5


async def test_lookup_failure_logs_safely_and_fails_closed() -> None:
    error = RuntimeError("SENSITIVE_TELEGRAM_ERROR")
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=error))
    resolver = ActorRoleResolver(bot)

    with patch("derp.tools.authorization.report_exception") as report:
        role = await resolver.resolve(
            chat_id=-100,
            chat_type="supergroup",
            user_id=42,
        )

    assert role is ActorRole.MEMBER
    report.assert_called_once_with(
        "actor_role_lookup_failed",
        exception=error,
        level="warning",
        **{
            "telegram.chat_id": -100,
            "telegram.user_id": 42,
        },
    )


async def test_invalidate_forces_next_lookup_to_be_live() -> None:
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            side_effect=[member("member"), member("administrator")]
        )
    )
    resolver = ActorRoleResolver(bot)

    assert (
        await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        is ActorRole.MEMBER
    )
    await resolver.invalidate(chat_id=-100, user_id=42)
    assert (
        await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        is ActorRole.ADMIN
    )
    assert bot.get_chat_member.await_count == 2


async def test_invalidation_during_lookup_prevents_stale_result_from_caching() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def lookup(chat_id: int, user_id: int) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return member("member")
        return member("administrator")

    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=lookup))
    resolver = ActorRoleResolver(bot)
    stale = asyncio.create_task(
        resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
    )
    await first_started.wait()

    await resolver.invalidate(chat_id=-100, user_id=42)
    fresh = await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
    release_first.set()

    assert fresh is ActorRole.ADMIN
    assert await stale is ActorRole.MEMBER
    assert (
        await resolver.resolve(chat_id=-100, chat_type="group", user_id=42)
        is ActorRole.ADMIN
    )
    assert bot.get_chat_member.await_count == 2


@pytest.mark.parametrize(
    ("cache_ttl", "max_cache_entries"),
    [
        (timedelta(0), 1),
        (timedelta(minutes=6), 1),
        (timedelta(seconds=1), 0),
        (timedelta(seconds=1), 10_001),
    ],
)
def test_cache_configuration_is_bounded(
    cache_ttl: timedelta,
    max_cache_entries: int,
) -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock())

    with pytest.raises(ValueError):
        ActorRoleResolver(
            bot,
            cache_ttl=cache_ttl,
            max_cache_entries=max_cache_entries,
        )
