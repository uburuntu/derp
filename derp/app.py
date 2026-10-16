"""Service composition: settings, database, bot, dispatcher and polling.

Importing this module creates nothing; ``main`` and ``build_dispatcher`` do.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Dispatcher
from pydantic_ai.models import Model
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.agent.runtime import ModelFactory, Runtime
from derp.billing.refunds import reconcile_refunds
from derp.config import Settings, get_settings
from derp.db.engine import Database
from derp.history.members import refresh_stale_profiles
from derp.history.middleware import CaptureMiddleware
from derp.history.transcription import Transcriber
from derp.inference.api import OpenRouterAPI
from derp.inference.openrouter import build_provider, chat_model
from derp.inference.pins import PinStore
from derp.inference.roles import RolePin
from derp.inference.scout import run_scout
from derp.money.wallets import expire_lots, release, stale_holds
from derp.ops import observability
from derp.ops.owner import notify_owners
from derp.ops.periodic import every
from derp.settings.privacy import enforce_retention
from derp.telegram.bot import create_bot
from derp.telegram.media import MediaGateway
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
    media: MediaGateway
    transcriber: Transcriber
    model_factory: ModelFactory = field(init=False)

    def __post_init__(self) -> None:
        self.model_factory = self.chat_model

    def chat_model(self, pin: RolePin) -> Model:
        return chat_model(pin, self.provider)

    @classmethod
    def create(cls, settings: Settings, db: Database, bot: Bot) -> Services:
        api = OpenRouterAPI(settings)
        pins = PinStore(db, settings)
        media = MediaGateway(bot)
        return cls(
            settings=settings,
            db=db,
            bot=bot,
            provider=build_provider(settings),
            api=api,
            pins=pins,
            media=media,
            transcriber=Transcriber(db=db, api=api, gateway=media, pins=pins, settings=settings),
        )

    async def close(self) -> None:
        await self.api.close()
        await self.bot.session.close()
        await self.db.close()


def build_dispatcher(services: Services) -> Dispatcher:
    runtime = Runtime(
        db=services.db,
        bot=services.bot,
        settings=services.settings,
        pins=services.pins,
        model_factory=services.model_factory,
        gateway=services.media,
        api=services.api,
    )
    dispatcher = Dispatcher(
        services=services,
        settings=services.settings,
        db=services.db,
        runtime=runtime,
        transcriber=services.transcriber,
    )
    dispatcher.update.outer_middleware(UpdateSpanMiddleware())
    dispatcher.message.outer_middleware(CaptureMiddleware())
    dispatcher.edited_message.outer_middleware(CaptureMiddleware(edits=True))
    from derp.routing import include_routers

    include_routers(dispatcher)
    return dispatcher


def background_tasks(services: Services, runtime: Runtime) -> list[asyncio.Task[None]]:
    async def scout() -> None:
        await run_scout(
            services.db,
            services.api,
            services.pins,
            services.settings,
            services.model_factory,
            notify=lambda text: notify_owners(services.bot, services.settings, text),
        )

    async def profiles() -> None:
        await refresh_stale_profiles(services.bot, services.db)

    async def refunds() -> None:
        if applied := await reconcile_refunds(services.db, services.bot):
            await notify_owners(services.bot, services.settings, f"Refund sweep applied {applied} refund(s).")

    async def free_stale_holds() -> None:
        # Holds outlive their run only when the process died mid-run; live runs keep theirs however long.
        cutoff = datetime.now(UTC) - timedelta(seconds=services.settings.run_caps.timeout_seconds + 600)
        async with services.db.session() as session:
            stale = await stale_holds(session, cutoff)
        for operation_id in stale:
            if operation_id in runtime.in_flight:
                continue
            async with services.db.transaction() as session:  # one wallet lock per transaction
                await release(session, operation_id)

    async def retention() -> None:
        await enforce_retention(services.db, services.settings)

    async def expire() -> None:
        async with services.db.transaction() as session:
            await expire_lots(session)

    return [
        asyncio.create_task(every("model-scout", 24 * 3600, scout, initial_delay=600)),
        asyncio.create_task(every("chat-profiles", 3600, profiles, initial_delay=120)),
        asyncio.create_task(every("stale-holds", 600, free_stale_holds, initial_delay=60)),
        asyncio.create_task(every("retention", 24 * 3600, retention, initial_delay=1800)),
        asyncio.create_task(every("expire-lots", 6 * 3600, expire, initial_delay=300)),
        asyncio.create_task(every("refund-sweep", 24 * 3600, refunds, initial_delay=900)),
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
        tasks = background_tasks(services, dispatcher["runtime"])
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
