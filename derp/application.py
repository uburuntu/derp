"""Telegram application assembly and runtime resource ownership."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
import logfire
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.chat_action import ChatActionMiddleware
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n.middleware import SimpleI18nMiddleware

from derp.artifacts import FilesystemArtifactStore
from derp.billing import (
    CommercePolicy,
    PaymentSettlementService,
    SubscriptionExpiryWorker,
)
from derp.config import Settings
from derp.db import DatabaseManager, init_db_manager
from derp.delivery import (
    MAX_TELEGRAM_PHOTO_BYTES,
    DeliveryService,
    ResendTokenCodec,
)
from derp.features import ImageFeatureService, ImageOperationCoordinator
from derp.handlers import (
    basic,
    chat,
    context_settings,
    credit_cmds,
    debug,
    donations,
    image,
    inline,
    payments,
    subscriptions,
    think,
    tts,
    video,
)
from derp.health import RuntimeHeartbeat
from derp.history.retention import HistoryRetentionWorker
from derp.llm.image_executor import PydanticAIImageExecutor
from derp.media import MediaGateway, TelegramImageSourceLoader
from derp.middlewares.api_persist import PersistBotActionsMiddleware
from derp.middlewares.api_resilient import ResilientRequestMiddleware
from derp.middlewares.commerce import CommerceMiddleware
from derp.middlewares.credit_service import CreditServiceMiddleware
from derp.middlewares.database_logger import DatabaseLoggerMiddleware
from derp.middlewares.db_models import DatabaseModelMiddleware
from derp.middlewares.event_context import EventContextMiddleware
from derp.middlewares.log_updates import LogUpdatesMiddleware
from derp.middlewares.sender import MessageSenderMiddleware
from derp.operations import (
    OperationLedger,
    OperationReconciler,
    OperationReconciliationWorker,
    QuoteEngine,
)
from derp.tools.authorization import ActorRoleResolver

logger = logging.getLogger(__name__)

APPLICATION_ROUTERS = (
    debug.router,
    context_settings.router,
    basic.router,
    donations.router,
    credit_cmds.router,
    think.router,
    payments.router,
    subscriptions.router,
    image.router,
    video.router,
    tts.router,
    inline.router,
    chat.router,
)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Resources that must remain alive while polling."""

    bot: Bot
    db: DatabaseManager
    media_gateway: MediaGateway
    actor_role_resolver: ActorRoleResolver
    artifact_store: FilesystemArtifactStore
    operation_ledger: OperationLedger
    delivery_service: DeliveryService
    image_operation_coordinator: ImageOperationCoordinator


def create_bot(settings: Settings) -> Bot:
    """Create the Telegram client without opening network resources."""
    default = DefaultBotProperties(
        parse_mode="HTML",
        disable_notification=True,
        protect_content=False,
        allow_sending_without_reply=True,
        link_preview_is_disabled=True,
    )
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=default,
    )


@asynccontextmanager
async def open_runtime(settings: Settings) -> AsyncIterator[Runtime]:
    """Open bot and database resources with cleanup on partial startup."""
    bot = create_bot(settings)
    db = init_db_manager(
        settings.database_url,
        echo=settings.environment == "dev",
    )

    async with AsyncExitStack() as stack:
        stack.push_async_callback(db.disconnect)
        await stack.enter_async_context(bot)
        media_client = await stack.enter_async_context(
            httpx.AsyncClient(follow_redirects=False)
        )
        await db.connect()
        media_gateway = MediaGateway(media_client)
        artifact_store = FilesystemArtifactStore(
            settings.artifact_store_path,
            max_item_bytes=MAX_TELEGRAM_PHOTO_BYTES,
        )
        operation_ledger = OperationLedger(db.session)
        delivery_service = DeliveryService(
            db.session,
            artifact_store,
            bot,
            operation_ledger,
            ResendTokenCodec(settings.callback_signing_key),
        )
        image_service = ImageFeatureService(
            PydanticAIImageExecutor(),
            source_loader=TelegramImageSourceLoader(media_gateway, bot=bot),
        )
        image_operation_coordinator = ImageOperationCoordinator(
            operation_ledger,
            QuoteEngine(),
            image_service,
            delivery_service,
        )
        await stack.enter_async_context(HistoryRetentionWorker(db))
        await stack.enter_async_context(
            SubscriptionExpiryWorker(PaymentSettlementService(db.session))
        )
        await stack.enter_async_context(
            OperationReconciliationWorker(
                OperationReconciler(db.session, delivery_service)
            )
        )
        yield Runtime(
            bot=bot,
            db=db,
            media_gateway=media_gateway,
            actor_role_resolver=ActorRoleResolver(bot),
            artifact_store=artifact_store,
            operation_ledger=operation_ledger,
            delivery_service=delivery_service,
            image_operation_coordinator=image_operation_coordinator,
        )


def create_dispatcher(
    runtime: Runtime,
    settings: Settings,
    logfire_instance: logfire.Logfire,
) -> Dispatcher:
    """Assemble middleware and routers for a configured runtime."""
    bot = runtime.bot
    db = runtime.db
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        media_gateway=runtime.media_gateway,
        actor_role_resolver=runtime.actor_role_resolver,
        operation_ledger=runtime.operation_ledger,
        delivery_service=runtime.delivery_service,
        image_operation_coordinator=runtime.image_operation_coordinator,
        commerce_policy=CommercePolicy(
            public_intake_enabled=settings.public_purchases_enabled
        ),
    )

    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    SimpleI18nMiddleware(i18n).setup(dispatcher)

    bot.session.middleware(ResilientRequestMiddleware(max_retries=3))
    bot.session.middleware(PersistBotActionsMiddleware(db=db))

    dispatcher.update.outer_middleware(LogUpdatesMiddleware(logfire_instance))
    dispatcher.update.outer_middleware(
        DatabaseLoggerMiddleware(
            db=db,
            bot_id=settings.bot_id,
            bot_username=settings.bot_username,
        )
    )

    dispatcher.update.middleware(EventContextMiddleware(db=db))
    dispatcher.update.middleware(DatabaseModelMiddleware(db=db))
    dispatcher.update.middleware(CommerceMiddleware(db=db))
    dispatcher.update.middleware(CreditServiceMiddleware(db=db))
    dispatcher.message.middleware(MessageSenderMiddleware())
    dispatcher.callback_query.middleware(MessageSenderMiddleware())
    dispatcher.message.middleware(ChatActionMiddleware())

    dispatcher.include_routers(*APPLICATION_ROUTERS)
    return dispatcher


async def run_application(
    settings: Settings,
    logfire_instance: logfire.Logfire,
) -> None:
    """Run Telegram polling for the lifetime of application resources."""
    async with open_runtime(settings) as runtime:
        dispatcher = create_dispatcher(runtime, settings, logfire_instance)
        bot_info = await runtime.bot.get_me()
        logger.info(
            "telegram_bot_starting",
            extra={"telegram.bot_id": bot_info.id},
        )

        async with RuntimeHeartbeat(settings.runtime_health_path):
            await dispatcher.start_polling(
                runtime.bot,
                allowed_updates=dispatcher.resolve_used_update_types()
                + ["edited_message"],
                close_bot_session=False,
                tasks_concurrency_limit=settings.polling_concurrency,
            )
