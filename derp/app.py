"""Service composition: settings, database, bot, dispatcher and polling.

Importing this module creates nothing; ``main`` and ``build_dispatcher`` do.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pydantic_ai
from aiogram import Bot, Dispatcher
from pydantic_ai.models import Model
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.agent.approvals import close_expired, drop_old
from derp.agent.runtime import ModelFactory, Runtime
from derp.billing.patrons import expire_hide_buttons
from derp.billing.purchases import recover_payments
from derp.billing.refunds import reconcile_refunds
from derp.config import Settings, get_settings
from derp.db.engine import Database
from derp.history.members import refresh_stale_profiles
from derp.history.middleware import CaptureMiddleware
from derp.history.transcription import Transcriber
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.openrouter import build_provider, chat_model
from derp.inference.pins import PinStore
from derp.inference.roles import RolePin
from derp.inference.scout import run_scout
from derp.money.wallets import expire_lots, release, stale_holds
from derp.ops import observability
from derp.ops.console import proposal_buttons
from derp.ops.owner import ErrorTally, notify_owners, send_error_digest
from derp.ops.periodic import every
from derp.scheduler.worker import Firings
from derp.settings.privacy import enforce_retention
from derp.settings.start import publish_commands
from derp.telegram.bot import create_bot
from derp.telegram.media import MediaGateway
from derp.telegram.middleware import UpdateSpanMiddleware
from derp.telegram.updates import ALLOWED_UPDATES
from derp.tools.lookups import Lookups
from derp.tools.workers import WorkerPool

log = logging.getLogger("derp.app")
ERROR_DIGEST_SECONDS = 600


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
    lookups: Lookups
    workers: WorkerPool
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
            transcriber=Transcriber(
                db=db,
                api=api,
                gateway=media,
                pins=pins,
                settings=settings,
                notify=lambda text: notify_owners(bot, settings, text),
            ),
            lookups=Lookups(settings),
            workers=WorkerPool(settings.media),
        )

    async def close(self) -> None:
        await self.api.close()
        await self.lookups.close()
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
        lookups=services.lookups,
        workers=services.workers,
        transcriber=services.transcriber,
    )
    dispatcher = Dispatcher(
        services=services,
        settings=services.settings,
        db=services.db,
        runtime=runtime,
        funder=runtime.funder,
        transcriber=services.transcriber,
        api=services.api,
        pins=services.pins,
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
            notify=lambda text, proposals: notify_owners(
                services.bot, services.settings, text, reply_markup=proposal_buttons(proposals)
            ),
        )

    async def profiles() -> None:
        await refresh_stale_profiles(services.bot, services.db)

    async def stars() -> None:
        for purchase in await recover_payments(services.db, services.bot):
            await notify_owners(
                services.bot,
                services.settings,
                f"⭐ Recovered a missed payment: {purchase.stars} · {purchase.product} · +{purchase.credits} dc",
            )
        if applied := await reconcile_refunds(services.db, services.bot, services.settings):
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

    firings = Firings()

    async def jobs() -> None:
        await firings.tick(services.db, services.bot, runtime)

    async def retention() -> None:
        await enforce_retention(services.db, services.settings)
        await drop_old(services.db, datetime.now(UTC) - timedelta(days=1))

    async def approvals() -> None:
        await close_expired(services.db, services.bot, datetime.now(UTC))

    tally = ErrorTally()
    logging.getLogger().addHandler(tally)

    async def errors() -> None:
        await send_error_digest(services.bot, services.settings, tally, ERROR_DIGEST_SECONDS // 60)

    async def openrouter_limit() -> None:
        try:
            key = await services.api.key_info()
        except OpenRouterError:
            return
        left = key.get("limit_remaining")
        if left is not None and float(left) < services.settings.openrouter_low_usd:
            await notify_owners(services.bot, services.settings, f"OpenRouter key: ${float(left):.2f} left.")

    async def patron_buttons() -> None:
        await expire_hide_buttons(services.db, services.bot)

    async def expire() -> None:
        async with services.db.transaction() as session:
            await expire_lots(session)

    return [
        asyncio.create_task(every("model-scout", 24 * 3600, scout, initial_delay=600)),
        asyncio.create_task(every("chat-profiles", 3600, profiles, initial_delay=120)),
        asyncio.create_task(every("stale-holds", 600, free_stale_holds, initial_delay=60)),
        asyncio.create_task(every("jobs", 20, jobs, initial_delay=15)),
        asyncio.create_task(every("retention", 24 * 3600, retention, initial_delay=1800)),
        asyncio.create_task(every("approvals", 300, approvals, initial_delay=90)),
        asyncio.create_task(every("expire-lots", 6 * 3600, expire, initial_delay=300)),
        asyncio.create_task(every("stars-sweep", 24 * 3600, stars, initial_delay=900)),
        asyncio.create_task(every("patron-buttons", 120, patron_buttons, initial_delay=90)),
        asyncio.create_task(
            every("error-digest", ERROR_DIGEST_SECONDS, errors, initial_delay=ERROR_DIGEST_SECONDS)
        ),
        asyncio.create_task(every("openrouter-limit", 24 * 3600, openrouter_limit, initial_delay=1200)),
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
        await publish_commands(services.bot, services.settings.owner_ids)
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
    pydantic_ai.BANNER_ENABLED = False  # Derp owns its output; runs emit their own spans
    observability.configure(settings)
    asyncio.run(run(settings))
