"""Main entry point for the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os

import logfire
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.chat_action import ChatActionMiddleware
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n.middleware import SimpleI18nMiddleware

from derp.config import settings
from derp.db import init_db_manager
from derp.handlers import (
    basic,
    chat,
    chat_settings,
    credit_cmds,
    debug,
    donations,
    image,
    inline,
    payments,
    think,
    tts,
    video,
)
from derp.middlewares.api_persist import PersistBotActionsMiddleware
from derp.middlewares.credit_service import CreditServiceMiddleware
from derp.middlewares.database_logger import DatabaseLoggerMiddleware
from derp.middlewares.db_models import DatabaseModelMiddleware
from derp.middlewares.event_context import EventContextMiddleware
from derp.middlewares.log_updates import LogUpdatesMiddleware

# Enable capturing LLM message content in spans
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")

# Configure logfire with basic settings
logfire.configure(
    token=settings.logfire_token,
    service_name=settings.app_name,
    environment=settings.environment,
    metrics=logfire.MetricsOptions(collect_in_spans=True),
)

# Auto-instrument integrations
logfire.instrument_pydantic_ai()  # Instruments Pydantic-AI agent runs and LLM calls
if settings.environment == "dev":
    logfire.instrument_httpx(capture_all=True)
logfire.instrument_system_metrics()
logfire.instrument_pydantic(record="failure")

logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logfire.LogfireLoggingHandler(),
    ],
)


async def main() -> None:
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
    logger.info(f"LLM Provider: {settings.default_llm_provider}")
    logger.info(f"Found {len(settings.google_api_keys)} Google API keys")

    dp = Dispatcher(storage=MemoryStorage())

    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    SimpleI18nMiddleware(i18n).setup(dp)

    # Initialize database
    db = init_db_manager(
        settings.database_url,
        echo=settings.environment == "dev",
    )
    await db.connect()

    # Persist all outbound API calls to messages table
    bot.session.middleware(PersistBotActionsMiddleware(db=db))

    # Outer middlewares (run before filters)
    dp.update.outer_middleware(LogUpdatesMiddleware())
    dp.update.outer_middleware(DatabaseLoggerMiddleware(db=db))

    # Inner middlewares (run after filters, before resolved handlers)
    dp.update.middleware(EventContextMiddleware(db=db))
    dp.update.middleware(DatabaseModelMiddleware(db=db))
    dp.update.middleware(CreditServiceMiddleware(db=db))
    dp.message.middleware(ChatActionMiddleware())

    dp.include_routers(
        debug.router,  # Admin debug commands (must be early to handle debug payments)
        basic.router,
        donations.router,
        chat_settings.router,
        credit_cmds.router,  # Credit management commands (/credits, /buy)
        think.router,  # Deep thinking handler
        payments.router,  # Telegram Stars payment handling
        image.router,
        video.router,
        tts.router,
        inline.router,
        # Must be the last one to handle all unhandled messages
        chat.router,
    )

    try:
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types() + ["edited_message"]
        )
    finally:
        await db.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("App stopped! Good bye.")
    except Exception:
        logfire.fatal("App crashed", _exc_info=True)
        raise
