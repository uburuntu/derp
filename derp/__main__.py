"""Main entry point for the Telegram bot."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

# Remove MemoryStorage import if no longer needed
# from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.i18n import I18n

# Import SimpleI18nMiddleware instead of FSMI18nMiddleware
from aiogram.utils.i18n.middleware import SimpleI18nMiddleware

from .common.utils import get_logger
from .config import settings
from .handlers import basic
from .middlewares.event_context import EventContextMiddleware
from .middlewares.log_updates import LogUpdatesMiddleware

logger = get_logger("Main")

# Configure i18n
i18n = I18n(path="derp/locales", default_locale="en", domain="messages")


async def main():
    default = DefaultBotProperties(
        parse_mode="HTML",
        disable_notification=True,
        protect_content=False,
        allow_sending_without_reply=True,
        link_preview_is_disabled=True,
    )

    bot = Bot(
        token=settings.telegram_bot_token,
        default=default,
    )

    # Log initial bot details
    bot_info = await bot.get_me()
    logger.info(
        f"Starting bot: {bot_info.full_name} (@{bot_info.username}) [ID: {bot_info.id}]"
    )
    logger.info(f"Environment: {settings.environment}")

    # Remove storage if not used elsewhere
    # dp = Dispatcher(storage=MemoryStorage())
    dp = Dispatcher()

    # Setup SimpleI18nMiddleware
    SimpleI18nMiddleware(i18n).setup(dp)

    # Remove FSM middleware registration
    # FSMI18nMiddleware(i18n).setup(dp)

    dp.update.outer_middleware(LogUpdatesMiddleware())
    dp.update.middleware(EventContextMiddleware())

    dp.include_routers(basic.router)

    if settings.environment != "prod":
        await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("App stopped! Good bye.")
