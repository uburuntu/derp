"""Milestone 1 journeys through the production Telegram dispatch boundary."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import logfire
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChatAdministrators,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberOwner,
    Message,
    PhotoSize,
    Update,
    User,
)
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.application import Runtime, create_dispatcher
from derp.config import settings
from derp.db import (
    approve_shared_fact,
    list_approved_shared_facts,
    propose_shared_fact,
)
from derp.features.chat_accounting import ChatTurnAccounting
from derp.handlers.context_settings import ContextAction, ContextCallback
from derp.media import MediaReference
from derp.models import Message as MessageModel
from derp.operations import OperationLedger
from derp.tools.policy import ActorRole

pytestmark = pytest.mark.database


@dataclass(frozen=True, slots=True)
class _AgentCall:
    source_message_id: int
    prompt: object
    history: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _RunResult:
    output: str

    def new_messages(self) -> list[object]:
        return []


@dataclass(slots=True)
class _RecordingAgent:
    calls: list[_AgentCall] = field(default_factory=list)

    @contextmanager
    def parallel_tool_call_execution_mode(self, _mode: str) -> Iterator[None]:
        yield

    async def run(self, prompt: object, **kwargs: Any) -> _RunResult:
        source_message_id = kwargs["deps"].message.message_id
        self.calls.append(
            _AgentCall(
                source_message_id=source_message_id,
                prompt=prompt,
                history=tuple(kwargs["message_history"]),
            )
        )
        return _RunResult(output=f"Journey response for {source_message_id}")


@dataclass(slots=True)
class _RecordingMediaGateway:
    payloads: dict[str, bytes]
    calls: list[str] = field(default_factory=list)

    async def download(
        self,
        *,
        reference: MediaReference,
        **_kwargs: Any,
    ) -> bytes:
        self.calls.append(reference.file_id)
        return self.payloads[reference.file_id]


class _RoleResolver:
    async def resolve(self, *, chat_type: str, **_kwargs: Any) -> ActorRole:
        if chat_type == "private":
            return ActorRole.PRIVATE_OWNER
        return ActorRole.ADMIN


class _TransactionDatabase:
    """Expose one rollback-owned test session through the runtime DB contract."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session
        await self._session.flush()

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


class _RecordingSession(BaseSession):
    """Execute Bot API methods locally while preserving their concrete requests."""

    def __init__(
        self,
        *,
        bot_user: User,
        admin_user: User,
        chat_types: dict[int, str],
    ) -> None:
        super().__init__()
        self.bot_user = bot_user
        self.admin_user = admin_user
        self.chat_types = chat_types
        self.requests: list[TelegramMethod[Any]] = []
        self.sent_messages: list[tuple[SendMessage, Message]] = []
        self._next_message_id = 90_000

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        del bot, timeout
        self.requests.append(method)
        if isinstance(method, GetMe):
            return self.bot_user
        if isinstance(method, GetChatAdministrators):
            return [ChatMemberOwner(user=self.admin_user, is_anonymous=False)]
        if isinstance(method, SendMessage):
            message = self._message_result(
                chat_id=int(method.chat_id),
                message_id=self._take_message_id(),
                text=method.text,
                thread_id=(
                    method.message_thread_id
                    if isinstance(method.message_thread_id, int)
                    else None
                ),
            )
            self.sent_messages.append((method, message))
            return message
        if isinstance(method, EditMessageText):
            return self._message_result(
                chat_id=int(method.chat_id),
                message_id=method.message_id,
                text=method.text,
                thread_id=None,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65_536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        del url, headers, timeout, chunk_size, raise_for_status
        yield b""

    def _take_message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    def _message_result(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        thread_id: int | None,
    ) -> Message:
        chat_type = self.chat_types[chat_id]
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=_chat(chat_id, chat_type, is_forum=thread_id is not None),
            from_user=self.bot_user,
            text=text,
            message_thread_id=thread_id,
            is_topic_message=thread_id is not None,
        )


def _chat(chat_id: int, chat_type: str, *, is_forum: bool = False) -> Chat:
    if chat_type == "private":
        return Chat(id=chat_id, type=chat_type, first_name="Ada")
    return Chat(
        id=chat_id,
        type=chat_type,
        title=f"Journey {abs(chat_id)}",
        is_forum=is_forum,
    )


def _message(
    *,
    message_id: int,
    user: User,
    chat_id: int,
    chat_type: str,
    text: str,
    thread_id: int | None = None,
    reply_to_message: Message | None = None,
    photo_file_id: str | None = None,
) -> Message:
    values: dict[str, object] = {
        "message_id": message_id,
        "date": datetime.now(UTC),
        "chat": _chat(chat_id, chat_type, is_forum=thread_id is not None),
        "from_user": user,
        "message_thread_id": thread_id,
        "is_topic_message": thread_id is not None,
        "reply_to_message": reply_to_message,
    }
    if photo_file_id is None:
        values["text"] = text
    else:
        values["caption"] = text
        values["photo"] = [
            PhotoSize(
                file_id=photo_file_id,
                file_unique_id=f"unique-{photo_file_id}",
                width=640,
                height=480,
                file_size=128,
            )
        ]
    return Message.model_validate(values)


@dataclass(slots=True)
class _Journey:
    dispatcher: Dispatcher
    bot: Bot
    telegram: _RecordingSession
    actor: User
    update_id: int = 0

    async def send_message(
        self,
        *,
        message_id: int,
        chat_id: int,
        chat_type: str,
        text: str,
        thread_id: int | None = None,
        reply_to_message: Message | None = None,
        photo_file_id: str | None = None,
    ) -> Message:
        message = _message(
            message_id=message_id,
            user=self.actor,
            chat_id=chat_id,
            chat_type=chat_type,
            text=text,
            thread_id=thread_id,
            reply_to_message=reply_to_message,
            photo_file_id=photo_file_id,
        )
        self.update_id += 1
        self.telegram.chat_types[chat_id] = chat_type
        await self.dispatcher.feed_update(
            self.bot,
            Update(update_id=self.update_id, message=message),
        )
        return message

    async def press(
        self,
        panel_message: Message,
        action: ContextAction,
    ) -> None:
        self.update_id += 1
        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                callback_query=CallbackQuery(
                    id=f"{action.value}-{self.update_id}",
                    from_user=self.actor,
                    chat_instance="m1-journey",
                    message=panel_message,
                    data=ContextCallback(action=action).pack(),
                ),
            ),
        )


def _history_binary_data(call: _AgentCall) -> list[bytes]:
    return [
        item.data
        for message in call.history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, list)
        for item in part.content
        if isinstance(item, BinaryContent)
    ]


def _history_text(call: _AgentCall) -> str:
    content: list[str] = []
    for message in call.history:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            values = part.content if isinstance(part.content, list) else [part.content]
            content.extend(value for value in values if isinstance(value, str))
    return "\n".join(content)


async def _scoped_message_count(
    session: AsyncSession,
    *,
    chat_id: UUID,
    thread_id: int,
) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(MessageModel)
            .where(
                MessageModel.chat_id == chat_id,
                MessageModel.thread_id == thread_id,
            )
        )
        or 0
    )


async def test_m1_dispatch_journey_matrix(
    db_session: AsyncSession,
    user_factory: Any,
    chat_factory: Any,
) -> None:
    admin_id = 7_701_001
    private_chat_id = admin_id
    mention_chat_id = -9_701_002
    reply_chat_id = -9_701_003
    forum_chat_id = -9_701_004
    deletion_chat_id = -9_701_005

    admin_model = await user_factory(
        telegram_id=admin_id,
        first_name="Ada",
        last_name="Admin",
        username="ada",
    )
    private_chat = await chat_factory(
        telegram_id=private_chat_id,
        chat_type="private",
        title=None,
        first_name="Ada",
        last_name="Admin",
    )
    mention_chat = await chat_factory(telegram_id=mention_chat_id)
    reply_chat = await chat_factory(telegram_id=reply_chat_id)
    forum_chat = await chat_factory(telegram_id=forum_chat_id, is_forum=True)
    deletion_chat = await chat_factory(telegram_id=deletion_chat_id, is_forum=True)
    for chat in (
        private_chat,
        mention_chat,
        reply_chat,
        forum_chat,
        deletion_chat,
    ):
        chat.context_notice_version = 1
    forum_chat.ambient_history_enabled = True
    deletion_chat.ambient_history_enabled = True
    await db_session.flush()

    admin = User(
        id=admin_id,
        is_bot=False,
        first_name="Ada",
        last_name="Admin",
        username="ada",
    )
    bot_user = User(
        id=settings.bot_id,
        is_bot=True,
        first_name="Derp",
        username=settings.bot_username,
        can_read_all_group_messages=True,
    )
    chat_types = {
        private_chat_id: "private",
        mention_chat_id: "supergroup",
        reply_chat_id: "supergroup",
        forum_chat_id: "supergroup",
        deletion_chat_id: "supergroup",
    }
    telegram = _RecordingSession(
        bot_user=bot_user,
        admin_user=admin,
        chat_types=chat_types,
    )
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=telegram,
    )
    bot._me = bot_user
    database = _TransactionDatabase(db_session)
    media_gateway = _RecordingMediaGateway(
        payloads={"topic-11-photo": b"topic-11-image-bytes"}
    )
    operation_ledger = OperationLedger(database.session)
    runtime = Runtime(
        bot=bot,
        db=database,
        media_gateway=media_gateway,
        actor_role_resolver=_RoleResolver(),
        artifact_store=MagicMock(),
        operation_ledger=operation_ledger,
        chat_turn_accounting=ChatTurnAccounting(operation_ledger),
        delivery_service=MagicMock(),
        image_operation_coordinator=MagicMock(),
        paid_media_operation_coordinator=MagicMock(),
        paid_media_approval_coordinator=MagicMock(),
        tts_paid_media_adapter=MagicMock(),
        inline_chat_service=MagicMock(),
        deferred_tool_approval_service=MagicMock(),
        operator_console=MagicMock(),
    )
    dispatcher = create_dispatcher(
        runtime,
        settings,
        logfire.DEFAULT_LOGFIRE_INSTANCE,
    )
    agent = _RecordingAgent()
    journey = _Journey(dispatcher, bot, telegram, admin)

    await dispatcher.emit_startup(bot=bot)
    try:
        with patch("derp.handlers.chat.create_chat_agent", return_value=agent):
            await journey.send_message(
                message_id=101,
                chat_id=private_chat_id,
                chat_type="private",
                text="Continue our private conversation",
            )
            await journey.send_message(
                message_id=201,
                chat_id=mention_chat_id,
                chat_type="supergroup",
                text="Derp, summarize this group discussion",
            )
            bot_reply = _message(
                message_id=300,
                user=bot_user,
                chat_id=reply_chat_id,
                chat_type="supergroup",
                text="Earlier response",
            )
            await journey.send_message(
                message_id=301,
                chat_id=reply_chat_id,
                chat_type="supergroup",
                text="Please continue",
                reply_to_message=bot_reply,
            )

            assert [call.source_message_id for call in agent.calls] == [101, 201, 301]

            await journey.send_message(
                message_id=401,
                chat_id=forum_chat_id,
                chat_type="supergroup",
                text="deployment dashboard",
                thread_id=11,
                photo_file_id="topic-11-photo",
            )
            await journey.send_message(
                message_id=402,
                chat_id=forum_chat_id,
                chat_type="supergroup",
                text="other topic secret",
                thread_id=22,
            )
            assert len(agent.calls) == 3

            await journey.send_message(
                message_id=403,
                chat_id=forum_chat_id,
                chat_type="supergroup",
                text="Derp, what is in the recent image?",
                thread_id=11,
            )

            assert [call.source_message_id for call in agent.calls] == [
                101,
                201,
                301,
                403,
            ]
            forum_call = agent.calls[-1]
            assert _history_binary_data(forum_call) == [b"topic-11-image-bytes"]
            assert "deployment dashboard" in _history_text(forum_call)
            assert "other topic secret" not in _history_text(forum_call)
            assert "what is in the recent image" not in _history_text(forum_call)
            assert "what is in the recent image" in str(forum_call.prompt)
            assert media_gateway.calls == ["topic-11-photo"]

            await journey.send_message(
                message_id=501,
                chat_id=deletion_chat_id,
                chat_type="supergroup",
                text="conversation history to clear",
                thread_id=33,
            )
            fact = await propose_shared_fact(
                db_session,
                chat_id=deletion_chat.id,
                thread_id=33,
                proposer_user_id=admin_model.id,
                fact_text="Approved facts survive history deletion.",
            )
            await approve_shared_fact(
                db_session,
                fact_id=fact.id,
                chat_id=deletion_chat.id,
                thread_id=33,
                admin_actor_id=admin_model.id,
            )

            await journey.send_message(
                message_id=502,
                chat_id=deletion_chat_id,
                chat_type="supergroup",
                text="/settings",
                thread_id=33,
            )
            panel_message = next(
                result
                for method, result in reversed(telegram.sent_messages)
                if method.text.startswith("<b>Derp</b>")
            )
            for action in (
                ContextAction.PRIVACY,
                ContextAction.CLEAR_CONFIRM,
                ContextAction.CLEAR,
            ):
                await journey.press(panel_message, action)

            assert (
                await _scoped_message_count(
                    db_session,
                    chat_id=deletion_chat.id,
                    thread_id=33,
                )
                == 0
            )
            assert [
                stored.id
                for stored in await list_approved_shared_facts(
                    db_session,
                    chat_id=deletion_chat.id,
                    thread_id=33,
                )
            ] == [fact.id]

            await journey.send_message(
                message_id=503,
                chat_id=deletion_chat_id,
                chat_type="supergroup",
                text="new history survives fact deletion",
                thread_id=33,
            )
            for action in (
                ContextAction.FORGET_FACTS_CONFIRM,
                ContextAction.FORGET_FACTS,
            ):
                await journey.press(panel_message, action)

            assert not await list_approved_shared_facts(
                db_session,
                chat_id=deletion_chat.id,
                thread_id=33,
            )
            assert (
                await _scoped_message_count(
                    db_session,
                    chat_id=deletion_chat.id,
                    thread_id=33,
                )
                == 1
            )

        journey_sends = [
            method
            for method, _result in telegram.sent_messages
            if method.text.startswith("Journey response")
        ]
        assert [method.reply_to_message_id for method in journey_sends] == [
            101,
            201,
            301,
            403,
        ]
        assert journey_sends[-1].message_thread_id == 11

        target_chat_ids = {
            private_chat.id,
            mention_chat.id,
            reply_chat.id,
            forum_chat.id,
        }
        inbound = list(
            await db_session.scalars(
                select(MessageModel).where(
                    MessageModel.chat_id.in_(target_chat_ids),
                    MessageModel.direction == "in",
                )
            )
        )
        capture_by_message = {
            stored.telegram_message_id: stored.capture_kind for stored in inbound
        }
        assert capture_by_message == {
            101: "explicit",
            201: "explicit",
            301: "explicit",
            401: "ambient",
            402: "ambient",
            403: "explicit",
        }
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(MessageModel)
                .where(
                    MessageModel.chat_id.in_(target_chat_ids),
                    MessageModel.direction == "out",
                )
            )
            == 4
        )

        callback_answers = [
            method.text
            for method in telegram.requests
            if isinstance(method, AnswerCallbackQuery) and method.text
        ]
        assert any(text.startswith("Deleted ") for text in callback_answers)
        assert "Forgot 1 approved fact" in callback_answers
    finally:
        await dispatcher.emit_shutdown(bot=bot)
        await bot.session.close()
