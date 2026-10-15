"""Owner pings: short plain-text DMs to the configured owners, best effort."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from derp.config import Settings

log = logging.getLogger("derp.owner")


async def notify_owners(bot: Bot, settings: Settings, text: str) -> None:
    for owner_id in settings.owner_ids:
        try:
            await bot.send_message(owner_id, text[:4000])
        except TelegramAPIError as error:  # the owner may not have started this bot
            log.info("owner ping not delivered: %s", type(error).__name__)
