"""Conversation-level harness over polling, HTTP, and independent DB sessions."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock, patch

import logfire
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from pydantic_ai import Agent, ModelMessage, ModelResponse, RunContext
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from derp.application import Runtime, create_dispatcher
from derp.billing import PaymentSettlementService, PaymentUpdateInboxService
from derp.config import Settings
from derp.db import DatabaseManager
from derp.execution import ExecutionPlan
from derp.features.chat_accounting import ChatTurnAccounting
from derp.history.service import process_native_history
from derp.inference import InferenceRecorder
from derp.inference_usage import InferenceUsageRepository
from derp.llm.deps import AgentDeps
from derp.llm.prompts import build_chat_system_prompt
from derp.models import Chat as ChatModel
from derp.models import ChatRunReceipt, Wallet, WalletLot
from derp.models import Message as MessageModel
from derp.models import User as UserModel
from derp.operations import OperationLedger
from derp.tools.authorization import ActorRoleResolver
from tests.e2e.telegram_api import BotAPICall, TelegramBotAPIServer


@dataclass(frozen=True, slots=True)
class TelegramUser:
    """A Telegram actor used to construct raw incoming updates."""

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str = "en"

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("Telegram user ID must be positive")

    def payload(self) -> dict[str, Any]:
        """Return the Bot API representation."""
        return {
            "id": self.id,
            "is_bot": False,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "language_code": self.language_code,
        }


@dataclass(frozen=True, slots=True)
class TelegramChat:
    """A private or group conversation scope."""

    id: int
    type: str
    title: str | None = None
    first_name: str | None = None
    is_forum: bool = False

    def __post_init__(self) -> None:
        if self.type == "private" and self.id <= 0:
            raise ValueError("private chat ID must be positive")
        if self.type != "private" and self.id >= 0:
            raise ValueError("group chat ID must be negative")

    def payload(self) -> dict[str, Any]:
        """Return the Bot API representation."""
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "is_forum": self.is_forum,
        }
        if self.type == "private":
            payload["first_name"] = self.first_name or "E2E user"
        else:
            payload["title"] = self.title or "E2E group"
        return payload


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One user action and its synchronization cursor."""

    update_id: int
    message: Mapping[str, Any]
    after_sequence: int

    @property
    def message_id(self) -> int:
        return int(self.message["message_id"])


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One complete Pydantic-AI request observed by the local model."""

    model_key: str
    messages: tuple[ModelMessage, ...]


@dataclass(slots=True)
class DeterministicChatModel:
    """Local Pydantic-AI model that records context and returns scripted text."""

    responses: deque[str]
    calls: list[ModelCall] = field(default_factory=list)

    @classmethod
    def from_responses(cls, *responses: str) -> DeterministicChatModel:
        if not responses or any(not response for response in responses):
            raise ValueError("at least one non-empty response is required")
        return cls(deque(responses))

    @classmethod
    def expecting_no_calls(cls) -> DeterministicChatModel:
        """Create a boundary that fails if conversational inference is reached."""
        return cls(deque())

    def create_agent(
        self,
        plan: ExecutionPlan,
    ) -> Agent[AgentDeps, str]:
        """Build a production-shaped Agent around deterministic local inference."""
        spec = plan.model

        async def respond(
            messages: list[ModelMessage],
            _info: AgentInfo,
        ) -> ModelResponse:
            self.calls.append(ModelCall(spec.key.value, tuple(messages)))
            if not self.responses:
                raise AssertionError("the deterministic model ran out of responses")
            return ModelResponse(
                parts=[TextPart(self.responses.popleft())],
                model_name=spec.provider_model_id,
                provider_name=spec.provider.value,
                provider_details={"cost": "0"},
            )

        agent: Agent[AgentDeps, str] = Agent(
            FunctionModel(respond, model_name=spec.provider_model_id),
            name="e2e-chat",
            deps_type=AgentDeps,
            output_type=str,
            capabilities=[ProcessHistory(process_native_history)],
        )

        @agent.instructions
        def add_chat_context(ctx: RunContext[AgentDeps]) -> str:
            return build_chat_system_prompt(ctx)

        return agent

    def assert_exhausted(self) -> None:
        """Require every scripted response to correspond to a model call."""
        if self.responses:
            raise AssertionError(
                f"{len(self.responses)} deterministic model responses were unused"
            )


@dataclass(slots=True)
class BlockingPaymentInbox:
    """Hold a payment update before durability so a process crash can be tested."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    _block: asyncio.Event = field(default_factory=asyncio.Event)

    async def persist(self, _envelope: object) -> None:
        self.started.set()
        await self._block.wait()
        raise AssertionError("blocking payment inbox was released unexpectedly")


@dataclass(slots=True)
class PollingApplication:
    """One restartable application process attached to the shared fake server."""

    dispatcher: Dispatcher
    bot: Bot
    model: DeterministicChatModel
    _factory_patch: Any
    _task: asyncio.Task[None]
    _closed: bool = False

    async def close(self) -> None:
        """Stop polling, propagate process failures, and close HTTP resources."""
        if self._closed:
            return
        self._closed = True
        try:
            if not self._task.done():
                await self.dispatcher.stop_polling()
            await asyncio.wait_for(self._task, timeout=5)
            if pending := tuple(self.dispatcher._handle_update_tasks):
                await asyncio.wait_for(asyncio.gather(*pending), timeout=5)
        finally:
            await self._cleanup()

    async def crash(self) -> None:
        """Terminate polling and in-flight handlers without a graceful drain."""
        if self._closed:
            raise RuntimeError("application is already closed")
        self._closed = True
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        pending = tuple(self.dispatcher._handle_update_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self._cleanup()

    async def _cleanup(self) -> None:
        try:
            self._factory_patch.stop()
        finally:
            await self.bot.session.close()
            # aiogram routers are module-level registries. A real process restart
            # reloads them; release them here so the next in-process restart does too.
            for router in self.dispatcher.sub_routers:
                router._parent_router = None
            self.dispatcher.sub_routers.clear()


class TelegramConversation:
    """Drive raw Telegram updates through a restartable production dispatcher."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: DatabaseManager,
        server: TelegramBotAPIServer,
    ) -> None:
        self.settings = settings
        self.database = database
        self.server = server
        self._application: PollingApplication | None = None
        self._next_update_id = 10_000
        self._next_callback_id = 0

    async def start(
        self,
        model: DeterministicChatModel,
        *,
        payment_update_inbox: PaymentUpdateInboxService
        | BlockingPaymentInbox
        | None = None,
    ) -> PollingApplication:
        """Start a fresh bot, dispatcher, and polling loop."""
        if self._application is not None:
            raise RuntimeError("application is already running")
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(self.server.base_url),
            timeout=2,
        )
        bot = Bot(
            token=self.settings.telegram_bot_token.get_secret_value(),
            session=session,
            default=DefaultBotProperties(
                parse_mode="HTML",
                disable_notification=True,
                protect_content=False,
                allow_sending_without_reply=True,
                link_preview_is_disabled=True,
            ),
        )
        runtime = self._build_runtime(
            bot,
            payment_update_inbox=payment_update_inbox,
        )
        dispatcher = create_dispatcher(
            runtime,
            self.settings,
            logfire.DEFAULT_LOGFIRE_INSTANCE,
        )
        factory_patch = patch(
            "derp.handlers.chat.create_chat_agent",
            side_effect=model.create_agent,
        )
        factory_patch.start()
        task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types()
                + ["edited_message"],
                polling_timeout=1,
                handle_signals=False,
                close_bot_session=False,
                tasks_concurrency_limit=self.settings.polling_concurrency,
            ),
            name="telegram-e2e-polling",
        )
        application = PollingApplication(
            dispatcher=dispatcher,
            bot=bot,
            model=model,
            _factory_patch=factory_patch,
            _task=task,
        )
        try:
            await self._wait_until_polling(application)
        except BaseException:
            await application.close()
            raise
        self._application = application
        return application

    async def restart(
        self,
        model: DeterministicChatModel,
    ) -> PollingApplication:
        """Replace the application process while retaining Telegram and DB state."""
        await self.stop()
        return await self.start(model)

    async def stop(self) -> None:
        """Stop the active process, if any."""
        application, self._application = self._application, None
        if application is not None:
            await application.close()

    async def crash(self) -> None:
        """Crash the active process while retaining server and database state."""
        application, self._application = self._application, None
        if application is None:
            raise RuntimeError("application is not running")
        await application.crash()

    async def seed_paid_scope(
        self,
        *,
        actor: TelegramUser,
        chat: TelegramChat,
        ambient_history_enabled: bool = False,
        member_status: str = "member",
        credits: int = 10_000_000,
    ) -> tuple[UserModel, ChatModel]:
        """Persist a funded scope and register its Telegram-side identities."""
        if credits <= 0:
            raise ValueError("credits must be positive")
        actor_payload = actor.payload()
        chat_payload = chat.payload()
        self.server.register_user(actor_payload)
        self.server.register_chat(chat_payload)
        self.server.set_member_status(
            chat_id=chat.id,
            user_id=actor.id,
            status=member_status,
        )
        self.server.set_member_status(
            chat_id=chat.id,
            user_id=self.settings.bot_id,
            status="administrator",
        )

        async with self.database.session() as session:
            user_model = UserModel(
                telegram_id=actor.id,
                is_bot=False,
                first_name=actor.first_name,
                last_name=actor.last_name,
                username=actor.username,
                language_code=actor.language_code,
                is_premium=False,
            )
            chat_model = ChatModel(
                telegram_id=chat.id,
                type=chat.type,
                title=chat.title,
                first_name=chat.first_name,
                is_forum=chat.is_forum,
                ambient_history_enabled=ambient_history_enabled,
                context_notice_version=1,
                shared_credit_spending_enabled=True,
            )
            session.add_all([user_model, chat_model])
            await session.flush()
            wallet = (
                Wallet(user_id=user_model.id)
                if chat.type == "private"
                else Wallet(chat_id=chat_model.id)
            )
            session.add(wallet)
            await session.flush()
            session.add(
                WalletLot(
                    wallet_id=wallet.id,
                    kind="purchased",
                    granted_credits=credits,
                    available_credits=credits,
                )
            )
        return user_model, chat_model

    async def send_text(
        self,
        *,
        actor: TelegramUser,
        chat: TelegramChat,
        text: str,
        thread_id: int | None = None,
        reply_to: Mapping[str, Any] | None = None,
        wait_persisted: bool = True,
    ) -> IncomingMessage:
        """Send text and wait for its transport acknowledgement and persistence."""
        self._require_running()
        if not text:
            raise ValueError("message text must not be empty")
        message_id = self.server.take_message_id(chat.id)
        message: dict[str, Any] = {
            "message_id": message_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": chat.payload(),
            "from": actor.payload(),
            "text": text,
        }
        if thread_id is not None:
            message["message_thread_id"] = thread_id
            message["is_topic_message"] = True
        if reply_to is not None:
            if int(reply_to["chat"]["id"]) != chat.id:
                raise ValueError("reply target must belong to the same chat")
            message["reply_to_message"] = dict(reply_to)
        self.server.register_message(message)
        after_sequence = len(self.server.calls)
        update_id = self._take_update_id()
        await self.server.push_update({"update_id": update_id, "message": message})
        await self.server.wait_confirmed(update_id)
        if wait_persisted:
            await self._wait_inbound_persisted(
                chat_id=chat.id,
                message_id=message_id,
            )
        self._raise_process_failure()
        return IncomingMessage(update_id, message, after_sequence)

    async def push_successful_payment(
        self,
        *,
        actor: TelegramUser,
        chat: TelegramChat,
        invoice_payload: str = "e2e-payment-payload",
    ) -> IncomingMessage:
        """Enqueue a successful Stars payment without waiting for acknowledgement."""
        self._require_running()
        message_id = self.server.take_message_id(chat.id)
        message: dict[str, Any] = {
            "message_id": message_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": chat.payload(),
            "from": actor.payload(),
            "successful_payment": {
                "currency": "XTR",
                "total_amount": 1,
                "invoice_payload": invoice_payload,
                "telegram_payment_charge_id": "e2e-telegram-charge",
                "provider_payment_charge_id": "e2e-provider-charge",
            },
        }
        self.server.register_message(message)
        after_sequence = len(self.server.calls)
        update_id = self._take_update_id()
        await self.server.push_update({"update_id": update_id, "message": message})
        return IncomingMessage(update_id, message, after_sequence)

    async def press_button(
        self,
        *,
        actor: TelegramUser,
        message: Mapping[str, Any],
        text: str,
        expect_edit: bool = True,
    ) -> Mapping[str, Any]:
        """Press a visible inline button and return the edited message state."""
        self._require_running()
        chat_id = int(message["chat"]["id"])
        message_id = int(message["message_id"])
        current = self.server.message(chat_id=chat_id, message_id=message_id)
        callback_data = self._callback_data(current, text)
        self._next_callback_id += 1
        callback_id = f"e2e-callback-{self._next_callback_id}"
        current = self.server.register_callback_query(
            callback_id=callback_id,
            chat_id=chat_id,
            message_id=message_id,
            callback_data=callback_data,
        )
        after_sequence = len(self.server.calls)
        update_id = self._take_update_id()
        await self.server.push_update(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": callback_id,
                    "from": actor.payload(),
                    "chat_instance": "telegram-e2e",
                    "message": current,
                    "data": callback_data,
                },
            }
        )
        await self.server.wait_confirmed(update_id)
        await self.server.wait_for_call(
            "answerCallbackQuery",
            after_sequence=after_sequence,
            predicate=lambda call: call.fields.get("callback_query_id") == callback_id,
        )
        await self.server.wait_callback_answered(callback_id)
        if expect_edit:
            await self.server.wait_for_call(
                "editMessageText",
                after_sequence=after_sequence,
                predicate=lambda call: (
                    int(call.fields.get("chat_id", 0)) == chat_id
                    and int(call.fields.get("message_id", 0)) == message_id
                ),
            )
        self._raise_process_failure()
        return self.server.message(chat_id=chat_id, message_id=message_id)

    async def expect_reply(
        self,
        incoming: IncomingMessage,
        *,
        text: str | None = None,
        wait_persisted: bool = False,
    ) -> Mapping[str, Any]:
        """Return the bot message that visibly replies to an incoming message."""
        await self.server.wait_for_call(
            "sendMessage",
            after_sequence=incoming.after_sequence,
            predicate=lambda call: (
                self._reply_target(call) == incoming.message_id
                and (text is None or call.fields.get("text") == text)
            ),
        )
        chat_id = int(incoming.message["chat"]["id"])
        reply = await self.server.wait_for_message(
            chat_id=chat_id,
            predicate=lambda message: (
                int(message.get("from", {}).get("id", 0)) == self.settings.bot_id
                and int(message.get("reply_to_message", {}).get("message_id", 0))
                == incoming.message_id
                and (text is None or message.get("text") == text)
            ),
        )
        if wait_persisted:
            await self._wait_message_persisted(
                chat_id=chat_id,
                message_id=int(reply["message_id"]),
                direction="out",
            )
        return reply

    async def expect_message(
        self,
        incoming: IncomingMessage,
        *,
        text: str | None = None,
        wait_persisted: bool = False,
    ) -> Mapping[str, Any]:
        """Return a bot message emitted for an update without requiring a reply."""
        await self.server.wait_for_call(
            "sendMessage",
            after_sequence=incoming.after_sequence,
            predicate=lambda call: (
                int(call.fields.get("chat_id", 0))
                == int(incoming.message["chat"]["id"])
            ),
        )
        chat_id = int(incoming.message["chat"]["id"])
        message = await self.server.wait_for_message(
            chat_id=chat_id,
            predicate=lambda candidate: (
                int(candidate.get("from", {}).get("id", 0)) == self.settings.bot_id
                and int(candidate.get("message_id", 0)) > incoming.message_id
                and (text is None or candidate.get("text") == text)
            ),
        )
        if wait_persisted:
            await self._wait_message_persisted(
                chat_id=chat_id,
                message_id=int(message["message_id"]),
                direction="out",
            )
        return message

    async def wait_run_receipt(
        self,
        *,
        chat_id: int,
        response_message_id: int,
        timeout: float = 5,
    ) -> None:
        """Wait until `/info` can resolve one delivered chat answer."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    async with self.database.read_session() as session:
                        stored = await session.scalar(
                            select(ChatRunReceipt.operation_id)
                            .join(ChatModel, ChatModel.id == ChatRunReceipt.chat_id)
                            .where(
                                ChatModel.telegram_id == chat_id,
                                ChatRunReceipt.response_message_ids.contains(
                                    [response_message_id]
                                ),
                            )
                        )
                    if stored is not None:
                        return
                    await asyncio.sleep(0.01)
        except TimeoutError as exc:
            raise AssertionError(
                f"run receipt for {(chat_id, response_message_id)} was not persisted"
            ) from exc

    async def latest_call(
        self,
        method: str,
        *,
        after_sequence: int = 0,
    ) -> BotAPICall:
        """Wait for and return the next named Bot API request."""
        return await self.server.wait_for_call(
            method,
            after_sequence=after_sequence,
        )

    def _build_runtime(
        self,
        bot: Bot,
        *,
        payment_update_inbox: PaymentUpdateInboxService | BlockingPaymentInbox | None,
    ) -> Runtime:
        ledger = OperationLedger(self.database.session)
        settlement = PaymentSettlementService(self.database.session)
        inference = InferenceRecorder(InferenceUsageRepository(self.database.session))
        inbox = payment_update_inbox or PaymentUpdateInboxService(
            self.database.session,
            settlement,
        )
        return Runtime(
            bot=bot,
            db=self.database,
            media_gateway=MagicMock(),
            actor_role_resolver=ActorRoleResolver(bot),
            artifact_store=MagicMock(),
            operation_ledger=ledger,
            chat_turn_accounting=ChatTurnAccounting(ledger),
            delivery_service=MagicMock(),
            image_operation_coordinator=MagicMock(),
            paid_media_operation_coordinator=MagicMock(),
            paid_media_approval_coordinator=MagicMock(),
            tts_paid_media_adapter=MagicMock(),
            inline_chat_service=MagicMock(),
            inference_recorder=inference,
            openrouter_client=None,
            inference_reconciliation=None,
            deferred_tool_approval_service=MagicMock(),
            operator_console=MagicMock(),
            operator_debug_refunds=MagicMock(),
            payment_update_inbox=cast(PaymentUpdateInboxService, inbox),
            payment_update_replay=MagicMock(),
            operator_debug_refund_replay=MagicMock(),
            subscription_renewal_replay=MagicMock(),
        )

    async def _wait_inbound_persisted(
        self,
        *,
        chat_id: int,
        message_id: int,
    ) -> None:
        await self._wait_message_persisted(
            chat_id=chat_id,
            message_id=message_id,
            direction="in",
        )

    async def _wait_message_persisted(
        self,
        *,
        chat_id: int,
        message_id: int,
        direction: str,
    ) -> None:
        try:
            async with asyncio.timeout(5):
                while True:
                    async with self.database.read_session() as session:
                        stored = await session.scalar(
                            select(MessageModel.id)
                            .join(ChatModel, MessageModel.chat_id == ChatModel.id)
                            .where(
                                ChatModel.telegram_id == chat_id,
                                MessageModel.telegram_message_id == message_id,
                                MessageModel.direction == direction,
                            )
                        )
                    if stored is not None:
                        return
                    await asyncio.sleep(0.01)
        except TimeoutError as exc:
            raise AssertionError(
                f"{direction} message {(chat_id, message_id)} was not persisted"
            ) from exc

    async def _wait_until_polling(self, application: PollingApplication) -> None:
        ready = asyncio.create_task(
            self.server.wait_for_call("getUpdates", timeout=5),
            name="telegram-e2e-ready",
        )
        done, _pending = await asyncio.wait(
            {ready, application._task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if application._task in done:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
            await application._task
            raise RuntimeError("polling stopped before its first getUpdates request")
        await ready

    def _take_update_id(self) -> int:
        self._next_update_id += 1
        return self._next_update_id

    def _require_running(self) -> PollingApplication:
        if self._application is None:
            raise RuntimeError("application is not running")
        self._raise_process_failure()
        return self._application

    def _raise_process_failure(self) -> None:
        if self._application is None or not self._application._task.done():
            return
        exception = self._application._task.exception()
        if exception is not None:
            raise RuntimeError("polling process failed") from exception
        raise RuntimeError("polling process stopped unexpectedly")

    @staticmethod
    def _reply_target(call: BotAPICall) -> int | None:
        parameters = call.fields.get("reply_parameters")
        if isinstance(parameters, Mapping) and parameters.get("message_id"):
            return int(parameters["message_id"])
        legacy = call.fields.get("reply_to_message_id")
        return int(legacy) if legacy else None

    @staticmethod
    def _callback_data(message: Mapping[str, Any], text: str) -> str:
        markup = message.get("reply_markup")
        rows = markup.get("inline_keyboard", []) if isinstance(markup, Mapping) else []
        matches = [
            button.get("callback_data")
            for row in rows
            for button in row
            if button.get("text") == text and button.get("callback_data")
        ]
        if len(matches) != 1:
            visible = [button.get("text") for row in rows for button in row]
            raise AssertionError(
                f"expected one {text!r} button, found {len(matches)}; visible={visible!r}"
            )
        return str(matches[0])


def model_text(messages: Sequence[ModelMessage]) -> str:
    """Render model request/response text for readable journey assertions."""
    parts: list[str] = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            values = content if isinstance(content, list) else [content]
            parts.extend(value for value in values if isinstance(value, str))
    return "\n".join(parts)


__all__ = [
    "BlockingPaymentInbox",
    "DeterministicChatModel",
    "IncomingMessage",
    "ModelCall",
    "PollingApplication",
    "TelegramChat",
    "TelegramConversation",
    "TelegramUser",
    "model_text",
]
