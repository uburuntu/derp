from types import SimpleNamespace
from typing import Any, cast

from aiogram import Bot

from derp.config import Settings
from derp.history.models import Chat
from derp.settings.onboarding import notice
from derp.settings.panel import kept_days


class StubBot:
    async def me(self) -> Any:
        return SimpleNamespace(username="DerpRobot", can_read_all_group_messages=True)


async def test_the_join_notice_states_the_configured_retention(settings: Settings) -> None:
    configured = settings.model_copy(
        update={"retention": settings.retention.model_copy(update={"group_days": 14})}
    )
    fresh = Chat(id=-1, type="supergroup", ambient=True)
    assert kept_days(fresh, configured) == 14
    text, _ = await notice(cast(Bot, StubBot()), configured, fresh, "en")
    assert "kept for 14 days" in text
    kept = Chat(id=-1, type="supergroup", ambient=True, retention_days=7)
    assert "kept for 7 days" in (await notice(cast(Bot, StubBot()), configured, kept, "en"))[0]
