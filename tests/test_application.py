"""Tests for application runtime ownership."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from derp.application import APPLICATION_ROUTERS, open_runtime
from derp.handlers import debug, donations, payments


def test_payment_router_order_preserves_donations_and_one_reconciliation_path() -> None:
    assert APPLICATION_ROUTERS.index(donations.router) < APPLICATION_ROUTERS.index(
        payments.router
    )
    assert APPLICATION_ROUTERS.index(debug.router) < APPLICATION_ROUTERS.index(
        payments.router
    )
    assert payments.reconciliation_router in payments.router.sub_routers


class FakeBot:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self):
        self.events.append("bot_enter")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.events.append("bot_close")


class FakeDatabase:
    def __init__(self, events: list[str], *, fail_connect: bool = False) -> None:
        self.events = events
        self.fail_connect = fail_connect

    async def connect(self) -> None:
        self.events.append("db_connect")
        if self.fail_connect:
            raise RuntimeError("database unavailable")

    async def disconnect(self) -> None:
        self.events.append("db_disconnect")


class FakeRetentionWorker:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self):
        self.events.append("retention_start")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.events.append("retention_stop")


@pytest.mark.asyncio
async def test_runtime_closes_bot_before_database() -> None:
    events: list[str] = []
    bot = FakeBot(events)
    database = FakeDatabase(events)
    settings = SimpleNamespace(database_url="postgresql://test", environment="dev")

    with (
        patch("derp.application.create_bot", return_value=bot),
        patch("derp.application.init_db_manager", return_value=database),
        patch(
            "derp.application.HistoryRetentionWorker",
            return_value=FakeRetentionWorker(events),
        ),
    ):
        async with open_runtime(settings) as runtime:
            assert runtime.bot is bot
            assert runtime.db is database
            events.append("running")

    assert events == [
        "bot_enter",
        "db_connect",
        "retention_start",
        "running",
        "retention_stop",
        "bot_close",
        "db_disconnect",
    ]


@pytest.mark.asyncio
async def test_runtime_cleans_up_after_partial_startup() -> None:
    events: list[str] = []
    bot = FakeBot(events)
    database = FakeDatabase(events, fail_connect=True)
    settings = SimpleNamespace(database_url="postgresql://test", environment="prod")

    with (
        patch("derp.application.create_bot", return_value=bot),
        patch("derp.application.init_db_manager", return_value=database),
        patch("derp.application.HistoryRetentionWorker") as retention_worker,
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        async with open_runtime(settings):
            pytest.fail("runtime should not open")

    retention_worker.assert_not_called()

    assert events == [
        "bot_enter",
        "db_connect",
        "bot_close",
        "db_disconnect",
    ]
