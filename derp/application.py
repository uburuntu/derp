"""Telegram application assembly and runtime resource ownership."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial

import httpx
import logfire
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.chat_action import ChatActionMiddleware
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n.middleware import SimpleI18nMiddleware

from derp import __version__
from derp.approvals.maintenance import DeferredApprovalExpiryWorker
from derp.approvals.paid_media import PaidMediaApprovalCoordinator
from derp.approvals.service import DeferredToolApprovalService
from derp.approvals.tokens import ApprovalTokenCodec
from derp.artifacts import FilesystemArtifactStore
from derp.billing import (
    CommercePolicy,
    PaymentSettlementService,
    PaymentUpdateInboxService,
    PaymentUpdateReplayWorker,
    SubscriptionExpiryWorker,
    SubscriptionManagementService,
    SubscriptionRenewalWorker,
)
from derp.billing.telegram import TelegramSubscriptionRenewalProvider
from derp.billing.telegram_updates import (
    DurablePaymentDispatcher,
    TelegramPaymentUpdateNotifier,
)
from derp.catalog import InferenceProvider, ModelRole
from derp.command_menu import configure_bot_command_menu
from derp.common.audio import convert_to_ogg_opus
from derp.config import Settings
from derp.db import DatabaseManager, init_db_manager
from derp.delivery import (
    MAX_TELEGRAM_FILE_BYTES,
    DeliveryMaintenanceWorker,
    DeliveryService,
    ResendTokenCodec,
)
from derp.execution import Feature, model_roles_for_features, plan_execution
from derp.features import (
    ImageFeatureService,
    ImageOperationCoordinator,
    ImageProviderRouter,
    InlineChatFeatureService,
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
    image,
    inline,
    legal_support,
    operator,
    paid_media_delivery,
    payments,
    premium_suspension,
    subscriptions,
    tts,
)
from derp.health import RuntimeHeartbeat
from derp.history.retention import HistoryRetentionWorker
from derp.inference import (
    InferenceRecorder,
    OpenRouterCostReconciliationService,
    OpenRouterCostReconciliationWorker,
)
from derp.inference_usage import InferenceUsageRepository
from derp.llm.image_executor import PydanticAIImageExecutor
from derp.llm.inline_executor import PydanticAIInlineExecutor
from derp.llm.providers import close_model_providers
from derp.llm.tts_executor import GoogleTtsExecutor
from derp.media import MediaGateway, TelegramImageSourceLoader
from derp.middlewares.api_persist import PersistBotActionsMiddleware
from derp.middlewares.api_resilient import ResilientRequestMiddleware
from derp.middlewares.database_logger import DatabaseLoggerMiddleware
from derp.middlewares.event_context import EventContextMiddleware
from derp.middlewares.log_updates import LogUpdatesMiddleware
from derp.middlewares.route_dependencies import setup_route_dependencies
from derp.middlewares.sender import MessageSenderMiddleware
from derp.openrouter import (
    OPENROUTER_IMAGE_VERTEX_ENDPOINT,
    OpenRouterClient,
    OpenRouterImageExecutor,
    OpenRouterImageRouteGuard,
)
from derp.operations import (
    OperationLedger,
    OperationReconciler,
    OperationReconciliationWorker,
    OperationRequestBinder,
    QuoteEngine,
)
from derp.operator import (
    OperatorAccessPolicy,
    OperatorConfirmationStore,
    OperatorConsoleService,
    OperatorControlConfig,
    OperatorDebugRefundService,
    OperatorDebugRefundWorker,
)
from derp.support import SupportRequestService, TermsAcceptanceService
from derp.tools.authorization import ActorRoleResolver

logger = logging.getLogger(__name__)

APPLICATION_ROUTERS = (
    operator.router,
    operator.rejection_router,
    debug.router,
    debug.rejection_router,
    legal_support.router,
    context_settings.router,
    basic.router,
    credit_cmds.router,
    premium_suspension.router,
    payments.router,
    subscriptions.router,
    paid_media_delivery.router,
    image.router,
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
    inline_chat_service: InlineChatFeatureService
    inference_recorder: InferenceRecorder
    openrouter_client: OpenRouterClient | None
    inference_reconciliation: OpenRouterCostReconciliationWorker | None
    deferred_tool_approval_service: DeferredToolApprovalService
    operator_console: OperatorConsoleService
    operator_debug_refunds: OperatorDebugRefundService
    payment_update_inbox: PaymentUpdateInboxService
    payment_update_replay: PaymentUpdateReplayWorker
    operator_debug_refund_replay: OperatorDebugRefundWorker
    subscription_renewal_replay: SubscriptionRenewalWorker


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
        stack.push_async_callback(close_model_providers)
        media_client = await stack.enter_async_context(
            httpx.AsyncClient(follow_redirects=False)
        )
        await db.connect()
        openrouter_client = None
        if settings.openrouter_api_key is not None:
            openrouter_client = await stack.enter_async_context(
                OpenRouterClient(
                    api_key=settings.openrouter_api_key.get_secret_value(),
                    app_url=settings.resolved_openrouter_app_url,
                    app_title=settings.openrouter_app_title,
                )
            )
        media_gateway = MediaGateway(media_client)
        artifact_store = FilesystemArtifactStore(
            settings.artifact_store_path,
            max_item_bytes=MAX_TELEGRAM_FILE_BYTES,
        )
        operation_ledger = OperationLedger(db.session)
        payment_settlement = PaymentSettlementService(db.session)
        payment_update_inbox = PaymentUpdateInboxService(
            db.session,
            payment_settlement,
        )
        payment_update_replay = await stack.enter_async_context(
            PaymentUpdateReplayWorker(
                payment_update_inbox,
                TelegramPaymentUpdateNotifier(bot),
            )
        )
        subscription_renewal_replay = await stack.enter_async_context(
            SubscriptionRenewalWorker(
                SubscriptionManagementService(db.session),
                TelegramSubscriptionRenewalProvider(bot),
            )
        )
        inference_usage = InferenceUsageRepository(db.session)
        inference_recorder = InferenceRecorder(inference_usage)
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
        image_executors = {
            InferenceProvider.GOOGLE: PydanticAIImageExecutor(),
        }
        if openrouter_client is not None:
            image_executors[InferenceProvider.OPENROUTER] = OpenRouterImageExecutor(
                openrouter_client,
                OpenRouterImageRouteGuard(
                    openrouter_client,
                    provider_tag=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
                ),
            )
        image_service = ImageFeatureService(
            ImageProviderRouter(image_executors),
            source_loader=TelegramImageSourceLoader(media_gateway, bot=bot),
        )
        image_operation_coordinator = ImageOperationCoordinator(
            operation_ledger,
            quote_engine,
            image_service,
            delivery_service,
            request_binder,
            inference_recorder=inference_recorder,
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
        tts_paid_media_adapter = TtsPaidMediaAdapter(
            TtsFeatureService(tts_executor),
            plan=plan_execution(
                Feature.TTS,
                ModelRole.TTS,
                provider=settings.inference_provider(Feature.TTS),
            ),
        )
        paid_media_approval_coordinator = PaidMediaApprovalCoordinator(
            paid_media_operation_coordinator,
            deferred_tool_approval_service,
            operation_ledger,
            request_binder,
        )
        inline_openrouter_enabled = (
            settings.uses_openrouter(Feature.INLINE_CHAT)
            and settings.openrouter_api_key is not None
        )
        inline_chat_service = InlineChatFeatureService(
            PydanticAIInlineExecutor(),
            inference_recorder,
            free_plan=(
                plan_execution(
                    Feature.INLINE_CHAT,
                    ModelRole.FREE_TEXT,
                    provider=InferenceProvider.OPENROUTER,
                )
                if inline_openrouter_enabled
                else None
            ),
        )
        inference_reconciliation = None
        if openrouter_client is not None:
            inference_reconciliation = await stack.enter_async_context(
                OpenRouterCostReconciliationWorker(
                    OpenRouterCostReconciliationService(
                        inference_usage,
                        openrouter_client,
                    )
                )
            )
        history_retention = await stack.enter_async_context(HistoryRetentionWorker(db))
        subscription_expiry = await stack.enter_async_context(
            SubscriptionExpiryWorker(payment_settlement)
        )
        operation_reconciliation = await stack.enter_async_context(
            OperationReconciliationWorker(
                OperationReconciler(db.session, delivery_service)
            )
        )
        delivery_maintenance = await stack.enter_async_context(
            DeliveryMaintenanceWorker(delivery_service)
        )
        approval_expiry = await stack.enter_async_context(
            DeferredApprovalExpiryWorker(deferred_tool_approval_service)
        )
        operator_debug_refunds = OperatorDebugRefundService(
            db.session,
            bot,
            payment_settlement,
        )
        operator_debug_refund_replay = await stack.enter_async_context(
            OperatorDebugRefundWorker(operator_debug_refunds)
        )
        operator_console = OperatorConsoleService(
            db,
            history_retention=history_retention,
            subscription_expiry=subscription_expiry,
            subscription_renewal=subscription_renewal_replay,
            payment_update_replay=payment_update_replay,
            debug_refund_reconciliation=operator_debug_refund_replay,
            operation_reconciliation=operation_reconciliation,
            delivery_maintenance=delivery_maintenance,
            approval_expiry=approval_expiry,
            inference_reconciliation=inference_reconciliation,
            openrouter_client=openrouter_client,
            enabled_model_roles=model_roles_for_features(
                settings.openrouter_enabled_features
            ),
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
            inline_chat_service=inline_chat_service,
            inference_recorder=inference_recorder,
            openrouter_client=openrouter_client,
            inference_reconciliation=inference_reconciliation,
            deferred_tool_approval_service=deferred_tool_approval_service,
            operator_console=operator_console,
            operator_debug_refunds=operator_debug_refunds,
            payment_update_inbox=payment_update_inbox,
            payment_update_replay=payment_update_replay,
            operator_debug_refund_replay=operator_debug_refund_replay,
            subscription_renewal_replay=subscription_renewal_replay,
        )


def create_dispatcher(
    runtime: Runtime,
    settings: Settings,
    logfire_instance: logfire.Logfire,
) -> Dispatcher:
    """Assemble middleware and routers for a configured runtime."""
    bot = runtime.bot
    db = runtime.db
    dispatcher = DurablePaymentDispatcher(
        storage=MemoryStorage(),
        payment_update_inbox=runtime.payment_update_inbox,
        media_gateway=runtime.media_gateway,
        actor_role_resolver=runtime.actor_role_resolver,
        operation_ledger=runtime.operation_ledger,
        chat_turn_accounting=runtime.chat_turn_accounting,
        delivery_service=runtime.delivery_service,
        image_operation_coordinator=runtime.image_operation_coordinator,
        paid_media_operation_coordinator=runtime.paid_media_operation_coordinator,
        paid_media_approval_coordinator=runtime.paid_media_approval_coordinator,
        tts_paid_media_adapter=runtime.tts_paid_media_adapter,
        inline_chat_service=runtime.inline_chat_service,
        inference_recorder=runtime.inference_recorder,
        deferred_tool_approval_service=runtime.deferred_tool_approval_service,
        operator_console=runtime.operator_console,
        operator_debug_refunds=runtime.operator_debug_refunds,
        support_requests=SupportRequestService(db.session),
        terms_acceptance=TermsAcceptanceService(db.session),
        operator_confirmations=OperatorConfirmationStore(),
        operator_access=OperatorAccessPolicy.from_ids(settings.operator_ids),
        operator_config=OperatorControlConfig(
            environment=settings.environment,
            service_version=__version__,
            public_purchases_enabled=settings.public_purchases_enabled,
            ai_content_capture_enabled=(
                settings.environment == "dev" and settings.logfire_capture_ai_content
            ),
            operator_ids=settings.operator_ids,
        ),
        commerce_policy=CommercePolicy(
            public_intake_enabled=settings.public_purchases_enabled
        ),
    )

    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    SimpleI18nMiddleware(i18n).setup(dispatcher)

    bot.session.middleware(ResilientRequestMiddleware(max_retries=3))
    bot.session.middleware(PersistBotActionsMiddleware(db=db))
    dispatcher.startup.register(
        partial(
            configure_bot_command_menu,
            i18n=i18n,
            public_purchases_enabled=settings.public_purchases_enabled,
            operator_ids=settings.operator_ids,
        )
    )

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
