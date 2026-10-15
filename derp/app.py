"""Service composition: settings, database, bot, dispatcher and polling.

Importing this module creates nothing; ``main`` and ``build_dispatcher`` do.
"""

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher

from derp.config import Settings, get_settings
from derp.db.engine import Database
from derp.ops import observability
from derp.telegram.bot import create_bot
from derp.telegram.middleware import UpdateSpanMiddleware
from derp.telegram.updates import ALLOWED_UPDATES

log = logging.getLogger("derp.app")


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    bot: Bot


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher(settings=services.settings, db=services.db)
    dispatcher.update.outer_middleware(UpdateSpanMiddleware())
    from derp.routing import include_routers

    include_routers(dispatcher)
    return dispatcher


async def run(settings: Settings) -> None:
    db = Database(settings.database_url, pool_size=settings.database_pool_size)
    bot = create_bot(settings)
    services = Services(settings=settings, db=db, bot=bot)
    try:
        dispatcher = build_dispatcher(services)
        me = await bot.get_me()
        log.info("polling as bot %s", me.id)
        await dispatcher.start_polling(bot, allowed_updates=list(ALLOWED_UPDATES), handle_signals=True)
    finally:
        await bot.session.close()
        await db.close()


def main() -> None:
    settings = get_settings()
    settings.validate_core()
    observability.configure(settings)
    asyncio.run(run(settings))
