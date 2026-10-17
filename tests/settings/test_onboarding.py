from types import SimpleNamespace
from typing import Any, cast

from aiogram import Bot

from derp.config import Settings
from derp.history.models import Chat
from derp.history.retention import kept_days
from derp.settings.onboarding import notice


class StubBot:
    def __init__(self, *, reads: bool = True) -> None:
        self.reads = reads

    async def me(self) -> Any:
        return SimpleNamespace(username="DerpRobot", can_read_all_group_messages=self.reads)


async def test_the_join_notice_states_the_configured_retention(settings: Settings) -> None:
    configured = settings.model_copy(
        update={"retention": settings.retention.model_copy(update={"group_days": 14})}
    )
    fresh = Chat(id=-1, type="supergroup", ambient=True)
    assert kept_days(fresh, configured.retention) == 14
    text, keyboard = await notice(cast(Bot, StubBot()), configured, fresh, "en")
    assert "kept for 14 days" in text and keyboard is None  # Derp already reads the chat: no admin link
    kept = Chat(id=-1, type="supergroup", ambient=True, retention_days=1)
    assert "kept for 1 day and" in (await notice(cast(Bot, StubBot()), configured, kept, "en"))[0]


async def test_the_admin_link_is_offered_while_derp_cant_read_the_chat(settings: Settings) -> None:
    blind = cast(Bot, StubBot(reads=False))
    text, keyboard = await notice(blind, settings, Chat(id=-1, type="supergroup", ambient=True), "en")
    assert "until an admin makes me an admin" in text
    assert keyboard is not None and keyboard.inline_keyboard[0][0].url is not None
    admin = Chat(id=-1, type="supergroup", ambient=True, bot_status="administrator")
    assert (await notice(blind, settings, admin, "en"))[1] is None
