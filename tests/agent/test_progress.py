import asyncio
from typing import Any, cast

from aiogram import Bot

from derp.agent.progress import ActionProgress, stage_for_tool
from derp.telegram.sender import Target


class ActionBot:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def send_chat_action(self, chat_id: int, action: str, **_: Any) -> bool:
        self.actions.append(action)
        return True


async def test_group_action_follows_the_stage_at_once() -> None:
    bot = ActionBot()
    progress = ActionProgress(cast(Bot, bot), Target(chat_id=-1))
    await progress.start()
    await asyncio.sleep(0)
    await progress.stage(stage_for_tool("generate_image"))
    await asyncio.sleep(0.01)  # well under the refresh interval: the change is shown immediately
    await progress.stage(stage_for_tool("search_history"))
    await asyncio.sleep(0.01)
    await progress.stage(stage_for_tool("speak"))
    await asyncio.sleep(0.01)
    await progress.close()
    assert bot.actions == ["typing", "upload_photo", "typing", "record_voice"]
