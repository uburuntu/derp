from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from derp.config import Settings
from derp.db.engine import Database
from derp.tools.lookups import Lookups
from tests.e2e.fake_api import FakeBotAPI
from tests.e2e.harness import Conversation, Derived
from tests.e2e.model import ScriptedModel

BOT_ID = 777000111


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[FakeBotAPI]:
    server = FakeBotAPI(
        token=settings.telegram_bot_token,
        bot_user={
            "id": BOT_ID,
            "is_bot": True,
            "first_name": "Derp",
            "username": "DerpRobot",
            "can_join_groups": True,
            "can_read_all_group_messages": True,
            "supports_inline_queries": True,
            "has_topics_enabled": False,
        },
    )
    await server.start()
    try:
        yield server
    finally:
        await server.close()
    server.assert_clean()


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def web() -> dict[str, Any]:
    """Canned JSON for the toolbox's web lookups, keyed by URL path; anything else fails like an outage."""
    return {}


def _offline(web: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = web.get(request.url.path)
        return httpx.Response(503) if body is None else httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


@pytest.fixture
async def conversation(
    settings: Settings,
    database: Database,
    api: FakeBotAPI,
    model: ScriptedModel,
    web: dict[str, Any],
    tmp_path: Path,
) -> AsyncIterator[Conversation]:
    configured = settings.model_copy(
        update={"telegram_api_base": api.base_url, "heartbeat_path": tmp_path / "poll"}
    )
    lookups = Derived(lambda s: Lookups(s.settings, client=httpx.AsyncClient(transport=_offline(web))))
    conv = Conversation(
        settings=configured,
        db=database,
        api=api,
        overrides={"model_factory": model.factory, "lookups": lookups},
    )
    await conv.start()
    try:
        yield conv
    finally:
        await conv.stop()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in str(item.path):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.db)
