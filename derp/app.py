"""Service composition: settings, database, bot, dispatcher and polling.

Importing this module creates nothing; ``main`` and ``build_dispatcher`` do.
"""

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.config import Settings, get_settings
from derp.db.engine import Database
from derp.history.members import refresh_stale_profiles
from derp.history.middleware import CaptureMiddleware
from derp.inference.api import OpenRouterAPI
from derp.inference.openrouter import build_provider, chat_model
from derp.inference.pins import PinStore
from derp.inference.scout import run_scout
from derp.ops import observability
from derp.ops.periodic import every
from derp.telegram.bot import create_bot
from derp.telegram.middleware import UpdateSpanMiddleware
from derp.telegram.updates import ALLOWED_UPDATES

log = logging.getLogger("derp.app")


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    bot: Bot
    provider: OpenRouterProvider
    api: OpenRouterAPI
    pins: PinStore

    @classmethod
    def create(cls, settings: Settings, db: Database, bot: Bot) -> Services:
        return cls(
            settings=settings,
            db=db,
            bot=bot,
            provider=build_provider(settings),
            api=OpenRouterAPI(settings),
            pins=PinStore(db, settings),
        )

    async def close(self) -> None:
        await self.api.close()
        await self.bot.session.close()
        await self.db.close()


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher(services=services, settings=services.settings, db=services.db)
    dispatcher.update.outer_middleware(UpdateSpanMiddleware())
    dispatcher.message.outer_middleware(CaptureMiddleware())
    dispatcher.edited_message.outer_middleware(CaptureMiddleware(edits=True))
    from derp.routing import include_routers

    include_routers(dispatcher)
    return dispatcher


def background_tasks(services: Services) -> list[asyncio.Task[None]]:
    async def scout() -> None:
        await run_scout(
            services.db,
            services.api,
            services.pins,
            services.settings,
            lambda pin: chat_model(pin, services.provider),
        )

    async def profiles() -> None:
        await refresh_stale_profiles(services.bot, services.db)

    return [
        asyncio.create_task(every("model-scout", 24 * 3600, scout, initial_delay=600)),
        asyncio.create_task(every("chat-profiles", 3600, profiles, initial_delay=120)),
    ]


async def run(settings: Settings) -> None:
    db = Database(settings.database_url, pool_size=settings.database_pool_size)
    services = Services.create(settings, db, create_bot(settings))
    tasks: list[asyncio.Task[None]] = []
    try:
        await services.pins.seed()
        dispatcher = build_dispatcher(services)
        me = await services.bot.get_me()
        log.info("polling as bot %s", me.id)
        tasks = background_tasks(services)
        await dispatcher.start_polling(
            services.bot, allowed_updates=list(ALLOWED_UPDATES), handle_signals=True
        )
    finally:
        for task in tasks:
            task.cancel()
        await services.close()


def main() -> None:
    settings = get_settings()
    settings.validate_core()
    observability.configure(settings)
    asyncio.run(run(settings))
