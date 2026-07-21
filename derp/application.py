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

from derp.approvals.maintenance import DeferredApprovalExpiryWorker
from derp.approvals.paid_media import PaidMediaApprovalCoordinator
from derp.approvals.service import DeferredToolApprovalService
from derp.approvals.tokens import ApprovalTokenCodec
from derp.artifacts import FilesystemArtifactStore
from derp.billing import (
    CommercePolicy,
    PaymentSettlementService,
    SubscriptionExpiryWorker,
)
from derp.common.audio import convert_to_ogg_opus
from derp.config import Settings
from derp.db import DatabaseManager, init_db_manager
from derp.delivery import (
    MAX_TELEGRAM_FILE_BYTES,
    DeliveryMaintenanceWorker,
    DeliveryService,
    ResendTokenCodec,
)
from derp.features import (
    ImageFeatureService,
    ImageOperationCoordinator,
    TtsFeatureService,
)
from derp.features.chat_accounting import ChatTurnAccounting
from derp.features.paid_media_operation import PaidMediaOperationCoordinator
from derp.features.tts_operation import TtsPaidMediaAdapter
from derp.handlers import (
    basic,
    chat,
    context_settings,
    credit_cmds,
    debug,
    donations,
    image,
    inline,
    paid_media_delivery,
    payments,
    subscriptions,
    think,
    tts,
    video,
)
from derp.health import RuntimeHeartbeat
from derp.history.retention import HistoryRetentionWorker
from derp.llm.image_executor import PydanticAIImageExecutor
from derp.llm.tts_executor import GoogleTtsExecutor
from derp.media import MediaGateway, TelegramImageSourceLoader
from derp.middlewares.api_persist import PersistBotActionsMiddleware
from derp.middlewares.api_resilient import ResilientRequestMiddleware
from derp.middlewares.database_logger import DatabaseLoggerMiddleware
from derp.middlewares.event_context import EventContextMiddleware
from derp.middlewares.log_updates import LogUpdatesMiddleware
from derp.middlewares.route_dependencies import setup_route_dependencies
from derp.middlewares.sender import MessageSenderMiddleware
from derp.operations import (
    OperationLedger,
    OperationReconciler,
    OperationReconciliationWorker,
    OperationRequestBinder,
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
    paid_media_delivery.router,
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
    chat_turn_accounting: ChatTurnAccounting
    delivery_service: DeliveryService
    image_operation_coordinator: ImageOperationCoordinator
    paid_media_operation_coordinator: PaidMediaOperationCoordinator
    paid_media_approval_coordinator: PaidMediaApprovalCoordinator
    tts_paid_media_adapter: TtsPaidMediaAdapter
    deferred_tool_approval_service: DeferredToolApprovalService


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
            max_item_bytes=MAX_TELEGRAM_FILE_BYTES,
        )
        operation_ledger = OperationLedger(db.session)
        chat_turn_accounting = ChatTurnAccounting(operation_ledger)
        delivery_service = DeliveryService(
            db.session,
            artifact_store,
            bot,
            operation_ledger,
            ResendTokenCodec(settings.callback_signing_key),
        )
        quote_engine = QuoteEngine()
        request_binder = OperationRequestBinder(settings.callback_signing_key)
        image_service = ImageFeatureService(
            PydanticAIImageExecutor(),
            source_loader=TelegramImageSourceLoader(media_gateway, bot=bot),
        )
        image_operation_coordinator = ImageOperationCoordinator(
            operation_ledger,
            quote_engine,
            image_service,
            delivery_service,
            request_binder,
        )
        paid_media_operation_coordinator = PaidMediaOperationCoordinator(
            operation_ledger,
            quote_engine,
            delivery_service,
            request_binder,
        )
        deferred_tool_approval_service = DeferredToolApprovalService(
            db.session,
            ApprovalTokenCodec(settings.callback_signing_key),
        )
        tts_executor = GoogleTtsExecutor(
            settings.google_api_paid_key.get_secret_value(),
            converter=convert_to_ogg_opus,
        )
        stack.push_async_callback(tts_executor.aclose)
        tts_paid_media_adapter = TtsPaidMediaAdapter(TtsFeatureService(tts_executor))
        paid_media_approval_coordinator = PaidMediaApprovalCoordinator(
            paid_media_operation_coordinator,
            deferred_tool_approval_service,
            operation_ledger,
            request_binder,
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
        await stack.enter_async_context(DeliveryMaintenanceWorker(delivery_service))
        await stack.enter_async_context(
            DeferredApprovalExpiryWorker(deferred_tool_approval_service)
        )
        yield Runtime(
            bot=bot,
            db=db,
            media_gateway=media_gateway,
            actor_role_resolver=ActorRoleResolver(bot),
            artifact_store=artifact_store,
            operation_ledger=operation_ledger,
            chat_turn_accounting=chat_turn_accounting,
            delivery_service=delivery_service,
            image_operation_coordinator=image_operation_coordinator,
            paid_media_operation_coordinator=paid_media_operation_coordinator,
            paid_media_approval_coordinator=paid_media_approval_coordinator,
            tts_paid_media_adapter=tts_paid_media_adapter,
            deferred_tool_approval_service=deferred_tool_approval_service,
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
        chat_turn_accounting=runtime.chat_turn_accounting,
        delivery_service=runtime.delivery_service,
        image_operation_coordinator=runtime.image_operation_coordinator,
        paid_media_operation_coordinator=runtime.paid_media_operation_coordinator,
        paid_media_approval_coordinator=runtime.paid_media_approval_coordinator,
        tts_paid_media_adapter=runtime.tts_paid_media_adapter,
        deferred_tool_approval_service=runtime.deferred_tool_approval_service,
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
    setup_route_dependencies(dispatcher, db)
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
