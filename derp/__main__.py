"""Main entry point for the Telegram bot."""

import asyncio
import logging

import logfire
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n.middleware import SimpleI18nMiddleware

from .common.database import get_database_client
from .config import settings
from .handlers import ai_response, basic
from .middlewares.database_logger import DatabaseLoggerMiddleware
from .middlewares.event_context import EventContextMiddleware
from .middlewares.log_updates import LogUpdatesMiddleware

# Configure logfire with basic settings
logfire.configure(
    token=settings.logfire_token,
    service_name=settings.app_name,
    environment=settings.environment,
)

logfire.instrument_pydantic_ai()
logfire.instrument_system_metrics()
logfire.instrument_pydantic(record="failure", include={"aiogram"})

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logfire.LogfireLoggingHandler(fallback=None),
    ],
)


async def main():
    logger = logging.getLogger("Main")

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

    bot_info = await bot.get_me()
    logger.info(
        f"Starting bot: {bot_info.full_name} (@{bot_info.username}) [ID: {bot_info.id}]"
    )
    logger.info(f"Environment: {settings.environment}")

    dp = Dispatcher(storage=MemoryStorage())

    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    SimpleI18nMiddleware(i18n).setup(dp)

    # Initialize middlewares
    db_middleware = DatabaseLoggerMiddleware()

    dp.update.outer_middleware(LogUpdatesMiddleware())
    dp.update.outer_middleware(DatabaseLoggerMiddleware())
    dp.update.middleware(EventContextMiddleware())

    dp.include_routers(basic.router, ai_response.router)

    try:
        await dp.start_polling(bot)
    finally:
        # Cleanup resources
        await db_middleware.close()
        db_client = get_database_client()
        await db_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("App stopped! Good bye.")
    except Exception:
        logfire.fatal("App crashed", _exc_info=True)
        raise
