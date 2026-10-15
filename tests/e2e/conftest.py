from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from derp.config import Settings
from derp.db.engine import Database
from tests.e2e.fake_api import FakeBotAPI
from tests.e2e.harness import Conversation

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
async def conversation(
    settings: Settings, database: Database, api: FakeBotAPI, tmp_path: Path
) -> AsyncIterator[Conversation]:
    configured = settings.model_copy(
        update={"telegram_api_base": api.base_url, "heartbeat_path": tmp_path / "poll"}
    )
    conv = Conversation(settings=configured, db=database, api=api)
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
