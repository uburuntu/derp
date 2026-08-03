"""Strict, in-process Telegram Bot API server for end-to-end tests."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    DeleteMyCommands,
    EditMessageText,
    GetChatAdministrators,
    GetChatMember,
    GetFile,
    GetMe,
    GetUpdates,
    SendChatAction,
    SendMessage,
    SetChatMenuButton,
    SetMessageReaction,
    SetMyCommands,
)
from aiogram.methods.base import TelegramMethod
from aiogram.types import Update
from aiohttp import web
from aiohttp.web_request import FileField

type JsonObject = dict[str, Any]
type CallPredicate = Callable[["BotAPICall"], bool]


@dataclass(frozen=True, slots=True)
class BotAPIAttachment:
    """One multipart attachment received from aiogram."""

    field: str
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class BotAPICall:
    """One decoded Bot API request in arrival order."""

    sequence: int
    method: str
    fields: Mapping[str, Any]
    attachments: tuple[BotAPIAttachment, ...] = ()

    def field(self, name: str) -> Any:
        """Return a required request field with a useful assertion failure."""
        if name not in self.fields:
            raise AssertionError(f"{self.method} did not include {name!r}")
        return self.fields[name]


@dataclass(frozen=True, slots=True)
class BotAPIFailure:
    """A queued Telegram error response for transport resilience tests."""

    error_code: int
    description: str
    http_status: int
    parameters: Mapping[str, Any] | None = None


_METHOD_TYPES: dict[str, type[TelegramMethod[Any]]] = {
    method.__api_method__: method
    for method in (
        AnswerCallbackQuery,
        DeleteMessage,
        DeleteMyCommands,
        EditMessageText,
        GetChatAdministrators,
        GetChatMember,
        GetFile,
        GetMe,
        GetUpdates,
        SendChatAction,
        SendMessage,
        SetChatMenuButton,
        SetMessageReaction,
        SetMyCommands,
    )
}

_TRUE_METHODS = frozenset(
    {
        DeleteMyCommands.__api_method__,
        SetChatMenuButton.__api_method__,
        SetMyCommands.__api_method__,
    }
)


class _TelegramHTMLTextParser(HTMLParser):
    """Project Telegram's supported HTML input to returned message text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class TelegramBotAPIServer:
    """A strict local Bot API implementation with real HTTP and JSON envelopes."""

    def __init__(
        self,
        *,
        token: str,
        bot_user: Mapping[str, Any],
        long_poll_cap: float = 0.25,
    ) -> None:
        if not token:
            raise ValueError("token must not be empty")
        if long_poll_cap <= 0:
            raise ValueError("long_poll_cap must be positive")
        self._token = token
        self._bot_user = dict(bot_user)
        self._long_poll_cap = long_poll_cap
        self._runner: web.AppRunner | None = None
        self._base_url: str | None = None
        self._condition = asyncio.Condition()
        self._calls: list[BotAPICall] = []
        self._protocol_errors: list[str] = []
        self._failures: dict[str, deque[BotAPIFailure]] = defaultdict(deque)
        self._updates: list[JsonObject] = []
        self._update_deliveries: dict[int, int] = defaultdict(int)
        self._confirmed_offset = 0
        self._chats: dict[int, JsonObject] = {}
        self._users: dict[int, JsonObject] = {int(self._bot_user["id"]): self._bot_user}
        self._member_statuses: dict[tuple[int, int], str] = {}
        self._messages: dict[tuple[int, int], JsonObject] = {}
        self._outstanding_callbacks: set[str] = set()
        self._next_message_ids: dict[int, int] = defaultdict(int)
        self._files: dict[str, tuple[str, bytes]] = {}

    @property
    def base_url(self) -> str:
        """Return the listening URL after startup."""
        if self._base_url is None:
            raise RuntimeError("Telegram Bot API server has not started")
        return self._base_url

    @property
    def calls(self) -> tuple[BotAPICall, ...]:
        """Return the immutable request journal snapshot."""
        return tuple(self._calls)

    @property
    def protocol_errors(self) -> tuple[str, ...]:
        """Return unsupported or malformed requests observed by the server."""
        return tuple(self._protocol_errors)

    @property
    def messages(self) -> tuple[JsonObject, ...]:
        """Return all live messages ordered by chat and message ID."""
        return tuple(
            deepcopy(message) for _key, message in sorted(self._messages.items())
        )

    @property
    def confirmed_offset(self) -> int:
        """Return the greatest getUpdates offset observed from the bot."""
        return self._confirmed_offset

    async def __aenter__(self) -> TelegramBotAPIServer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Bind an ephemeral loopback port."""
        if self._runner is not None:
            raise RuntimeError("Telegram Bot API server is already running")
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle_api)
        app.router.add_get("/file/bot{token}/{path:.*}", self._handle_file)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        addresses = runner.addresses
        if len(addresses) != 1:
            await runner.cleanup()
            raise RuntimeError(f"expected one server address, got {addresses!r}")
        host, port = addresses[0][:2]
        self._runner = runner
        self._base_url = f"http://{host}:{port}"

    async def close(self) -> None:
        """Stop accepting requests and release the listening socket."""
        runner, self._runner = self._runner, None
        self._base_url = None
        if runner is not None:
            await runner.cleanup()

    def register_user(self, user: Mapping[str, Any]) -> None:
        """Register a user that may appear in updates or membership results."""
        self._users[int(user["id"])] = dict(user)

    def register_chat(self, chat: Mapping[str, Any]) -> None:
        """Register a chat used by incoming and outgoing messages."""
        self._chats[int(chat["id"])] = dict(chat)

    def set_member_status(self, *, chat_id: int, user_id: int, status: str) -> None:
        """Configure a deterministic getChatMember result."""
        self._member_statuses[(chat_id, user_id)] = status

    def register_file(
        self, file_id: str, data: bytes, *, path: str | None = None
    ) -> str:
        """Register bytes for getFile and the file download endpoint."""
        file_path = path or f"fixtures/{file_id}"
        self._files[file_id] = (file_path, data)
        return file_path

    def register_message(self, message: Mapping[str, Any]) -> None:
        """Register a raw Telegram message for replies and callbacks."""
        parsed = deepcopy(dict(message))
        chat_id = int(parsed["chat"]["id"])
        message_id = int(parsed["message_id"])
        self._messages[(chat_id, message_id)] = parsed
        self._next_message_ids[chat_id] = max(
            self._next_message_ids[chat_id], message_id
        )

    def take_message_id(self, chat_id: int) -> int:
        """Allocate Telegram's chat-wide monotonic message ID."""
        self._required_chat(chat_id)
        self._next_message_ids[chat_id] += 1
        return self._next_message_ids[chat_id]

    def message(self, *, chat_id: int, message_id: int) -> JsonObject:
        """Return the current server-side message, rejecting stale targets."""
        try:
            return deepcopy(self._messages[(chat_id, message_id)])
        except KeyError as exc:
            raise AssertionError(
                f"Telegram message {(chat_id, message_id)} is not live"
            ) from exc

    def register_callback_query(
        self,
        *,
        callback_id: str,
        chat_id: int,
        message_id: int,
        callback_data: str,
    ) -> JsonObject:
        """Register one callback only when its button is currently visible."""
        if callback_id in self._outstanding_callbacks:
            raise ValueError(f"duplicate callback query {callback_id!r}")
        message = self.message(chat_id=chat_id, message_id=message_id)
        markup = message.get("reply_markup")
        rows = markup.get("inline_keyboard", []) if isinstance(markup, Mapping) else []
        visible_data = {
            button.get("callback_data")
            for row in rows
            for button in row
            if button.get("callback_data")
        }
        if callback_data not in visible_data:
            raise AssertionError("callback data is not present on the live message")
        self._outstanding_callbacks.add(callback_id)
        return message

    def update_delivery_count(self, update_id: int) -> int:
        """Return how many getUpdates responses contained this update."""
        return self._update_deliveries[update_id]

    async def push_update(self, update: Mapping[str, Any]) -> int:
        """Validate and enqueue one Bot API-shaped update."""
        raw = dict(update)
        Update.model_validate(raw)
        update_id = int(raw["update_id"])
        async with self._condition:
            if any(int(item["update_id"]) == update_id for item in self._updates):
                raise ValueError(f"duplicate update_id {update_id}")
            self._updates.append(raw)
            self._updates.sort(key=lambda item: int(item["update_id"]))
            self._condition.notify_all()
        return update_id

    def fail_next(
        self,
        method: str,
        *,
        error_code: int,
        description: str,
        http_status: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        """Queue one well-formed Telegram error for a named API method."""
        if method not in _METHOD_TYPES:
            raise ValueError(f"unsupported Bot API method {method!r}")
        self._failures[method].append(
            BotAPIFailure(
                error_code=error_code,
                description=description,
                http_status=http_status or error_code,
                parameters=dict(parameters) if parameters else None,
            )
        )

    async def wait_for_call(
        self,
        method: str,
        *,
        after_sequence: int = 0,
        predicate: CallPredicate | None = None,
        timeout: float = 5,
    ) -> BotAPICall:
        """Wait for a matching request without introducing timing sleeps."""

        def match() -> BotAPICall | None:
            return next(
                (
                    call
                    for call in self._calls
                    if call.sequence > after_sequence
                    and call.method == method
                    and (predicate is None or predicate(call))
                ),
                None,
            )

        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: match() is not None),
                    timeout,
                )
            except TimeoutError as exc:
                observed = [
                    call for call in self._calls if call.sequence > after_sequence
                ]
                recent = (
                    observed if len(observed) <= 12 else [*observed[:4], *observed[-8:]]
                )
                summary = ", ".join(
                    f"{call.sequence}:{call.method}:{dict(call.fields)!r}"
                    for call in recent
                )
                raise AssertionError(
                    f"timed out waiting for {method}; recent calls: {summary or 'none'}"
                ) from exc
            call = match()
            assert call is not None
            return call

    async def wait_confirmed(self, update_id: int, *, timeout: float = 5) -> None:
        """Wait until polling acknowledges an update with a higher offset."""
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self._confirmed_offset >= update_id + 1
                    ),
                    timeout,
                )
            except TimeoutError as exc:
                pending = [int(item["update_id"]) for item in self._updates]
                raise AssertionError(
                    "timed out waiting for update confirmation "
                    f"{update_id}; offset={self._confirmed_offset}, pending={pending}"
                ) from exc

    async def wait_for_message(
        self,
        *,
        chat_id: int,
        predicate: Callable[[Mapping[str, Any]], bool],
        timeout: float = 5,
    ) -> JsonObject:
        """Wait for a live message matching a user-visible predicate."""

        def match() -> JsonObject | None:
            return next(
                (
                    message
                    for (candidate_chat_id, _message_id), message in reversed(
                        self._messages.items()
                    )
                    if candidate_chat_id == chat_id and predicate(message)
                ),
                None,
            )

        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: match() is not None), timeout
                )
            except TimeoutError as exc:
                raise AssertionError(
                    f"timed out waiting for a message in chat {chat_id}"
                ) from exc
            message = match()
            assert message is not None
            return deepcopy(message)

    async def wait_callback_answered(
        self,
        callback_id: str,
        *,
        timeout: float = 5,
    ) -> None:
        """Wait until Telegram has accepted an answer for one callback query."""
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: callback_id not in self._outstanding_callbacks
                    ),
                    timeout,
                )
            except TimeoutError as exc:
                raise AssertionError(
                    f"callback query {callback_id!r} was not answered"
                ) from exc

    def assert_clean(self) -> None:
        """Fail teardown when the application crossed an unsupported API surface."""
        if self._protocol_errors:
            raise AssertionError("; ".join(self._protocol_errors))
        if self._outstanding_callbacks:
            raise AssertionError(
                f"unanswered callback queries: {sorted(self._outstanding_callbacks)!r}"
            )

    async def _handle_api(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        method = request.match_info["method"]
        if token != self._token:
            return self._error(401, "Unauthorized", http_status=401)
        method_type = _METHOD_TYPES.get(method)
        if method_type is None:
            self._protocol_errors.append(f"unsupported Bot API method {method!r}")
            return self._error(404, f"Method {method} not found", http_status=404)

        try:
            fields, attachments = await self._decode_request(request, method_type)
            method_type.model_validate(fields)
        except Exception as exc:
            self._protocol_errors.append(f"invalid {method} request: {exc}")
            return self._error(400, f"Bad Request: invalid {method}")

        async with self._condition:
            call = BotAPICall(
                sequence=len(self._calls) + 1,
                method=method,
                fields=MappingProxyType(deepcopy(fields)),
                attachments=attachments,
            )
            self._calls.append(call)
            self._condition.notify_all()

        if failures := self._failures.get(method):
            failure = failures.popleft()
            return self._error(
                failure.error_code,
                failure.description,
                http_status=failure.http_status,
                parameters=failure.parameters,
            )

        try:
            result = await self._dispatch(method, fields)
        except Exception as exc:
            self._protocol_errors.append(f"failed to serve {method}: {exc}")
            return self._error(400, f"Bad Request: {exc}")
        return web.json_response({"ok": True, "result": result})

    async def _handle_file(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["token"] != self._token:
            raise web.HTTPUnauthorized()
        path = request.match_info["path"]
        match = next(
            (
                data
                for registered_path, data in self._files.values()
                if registered_path == path
            ),
            None,
        )
        if match is None:
            raise web.HTTPNotFound()
        return web.Response(body=match, content_type="application/octet-stream")

    async def _decode_request(
        self,
        request: web.Request,
        method_type: type[TelegramMethod[Any]],
    ) -> tuple[JsonObject, tuple[BotAPIAttachment, ...]]:
        form = await request.post()
        raw_fields: JsonObject = {}
        attachments: list[BotAPIAttachment] = []
        for name, value in form.items():
            if isinstance(value, FileField):
                attachments.append(
                    BotAPIAttachment(
                        field=name,
                        filename=value.filename,
                        content_type=value.content_type,
                        data=value.file.read(),
                    )
                )
                continue
            raw_fields[name] = self._decode_value(value)

        attachment_names = {attachment.field for attachment in attachments}
        declared_fields = set(method_type.model_fields)
        unknown = set(raw_fields) - declared_fields
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)!r}")
        references = self._attachment_references(raw_fields)
        if references != attachment_names:
            raise ValueError(
                "multipart references do not match attachments: "
                f"references={sorted(references)!r}, files={sorted(attachment_names)!r}"
            )
        return raw_fields, tuple(attachments)

    @staticmethod
    def _decode_value(value: str) -> Any:
        stripped = value.strip()
        if stripped.startswith(("{", "[")) or stripped in {"true", "false", "null"}:
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        return value

    @classmethod
    def _attachment_references(cls, value: object) -> set[str]:
        if isinstance(value, str):
            return (
                {value.removeprefix("attach://")}
                if value.startswith("attach://")
                else set()
            )
        if isinstance(value, Mapping):
            return set().union(
                *(cls._attachment_references(item) for item in value.values())
            )
        if isinstance(value, list | tuple):
            return set().union(*(cls._attachment_references(item) for item in value))
        return set()

    async def _dispatch(self, method: str, fields: JsonObject) -> Any:
        if method == GetMe.__api_method__:
            return self._bot_user
        if method == GetUpdates.__api_method__:
            return await self._get_updates(fields)
        if method == SendMessage.__api_method__:
            return await self._send_message(fields)
        if method == EditMessageText.__api_method__:
            return await self._edit_message_text(fields)
        if method == DeleteMessage.__api_method__:
            return self._delete_message(fields)
        if method == SendChatAction.__api_method__:
            self._required_chat(int(fields["chat_id"]))
            return True
        if method == SetMessageReaction.__api_method__:
            key = (int(fields["chat_id"]), int(fields["message_id"]))
            if key not in self._messages:
                raise ValueError(f"message {key} does not exist")
            return True
        if method == AnswerCallbackQuery.__api_method__:
            return await self._answer_callback_query(fields)
        if method == GetChatMember.__api_method__:
            return self._get_chat_member(fields)
        if method == GetChatAdministrators.__api_method__:
            return self._get_chat_administrators(fields)
        if method == GetFile.__api_method__:
            return self._get_file(fields)
        if method in _TRUE_METHODS:
            return True
        raise RuntimeError(f"no response handler for {method}")

    async def _get_updates(self, fields: JsonObject) -> list[JsonObject]:
        offset = int(fields.get("offset", 0))
        limit = int(fields.get("limit", 100))
        timeout = min(float(fields.get("timeout", 0)), self._long_poll_cap)
        allowed = fields.get("allowed_updates")
        allowed_types = set(allowed) if isinstance(allowed, list) else None

        async with self._condition:
            if offset:
                self._confirmed_offset = max(self._confirmed_offset, offset)
                self._updates = [
                    update
                    for update in self._updates
                    if int(update["update_id"]) >= offset
                ]
                self._condition.notify_all()

            def available() -> list[JsonObject]:
                return [
                    update
                    for update in self._updates
                    if int(update["update_id"]) >= offset
                    and (
                        allowed_types is None
                        or any(
                            key in allowed_types for key in update if key != "update_id"
                        )
                    )
                ][:limit]

            if not available() and timeout > 0:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: bool(available())), timeout
                    )
                except TimeoutError:
                    pass
            batch = available()
            for update in batch:
                self._update_deliveries[int(update["update_id"])] += 1
            return [dict(update) for update in batch]

    async def _send_message(self, fields: JsonObject) -> JsonObject:
        chat_id = int(fields["chat_id"])
        chat = self._required_chat(chat_id)
        message_id = self.take_message_id(chat_id)
        message: JsonObject = {
            "message_id": message_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": chat,
            "from": self._bot_user,
            "text": self._rendered_text(fields),
        }
        if thread_id := fields.get("message_thread_id"):
            message["message_thread_id"] = int(thread_id)
            message["is_topic_message"] = True
        if markup := fields.get("reply_markup"):
            message["reply_markup"] = markup
        reply_parameters = fields.get("reply_parameters")
        reply_message_id = (
            int(reply_parameters["message_id"])
            if isinstance(reply_parameters, Mapping)
            else (
                int(fields["reply_to_message_id"])
                if fields.get("reply_to_message_id")
                else None
            )
        )
        if reply_message_id is not None:
            replied = self._messages.get((chat_id, reply_message_id))
            if replied is not None:
                message["reply_to_message"] = self._shallow_message(replied)
        async with self._condition:
            self.register_message(message)
            self._condition.notify_all()
        return message

    async def _edit_message_text(self, fields: JsonObject) -> JsonObject:
        if fields.get("inline_message_id"):
            raise ValueError("inline message is not registered")
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        async with self._condition:
            if key not in self._messages:
                raise ValueError(f"message {key} does not exist")
            message = self._messages[key]
            message["text"] = self._rendered_text(fields)
            if markup := fields.get("reply_markup"):
                message["reply_markup"] = markup
            else:
                message.pop("reply_markup", None)
            self._condition.notify_all()
            return dict(message)

    def _delete_message(self, fields: JsonObject) -> bool:
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        if key not in self._messages:
            raise ValueError(f"message {key} does not exist")
        del self._messages[key]
        return True

    async def _answer_callback_query(self, fields: JsonObject) -> bool:
        callback_id = str(fields["callback_query_id"])
        async with self._condition:
            if callback_id not in self._outstanding_callbacks:
                raise ValueError(f"callback query {callback_id!r} is not outstanding")
            self._outstanding_callbacks.remove(callback_id)
            self._condition.notify_all()
            return True

    def _get_chat_member(self, fields: JsonObject) -> JsonObject:
        chat_id = int(fields["chat_id"])
        user_id = int(fields["user_id"])
        self._required_chat(chat_id)
        user = self._required_user(user_id)
        status = self._member_statuses.get((chat_id, user_id), "member")
        result: JsonObject = {"status": status, "user": user}
        if status == "creator":
            result["is_anonymous"] = False
        elif status == "administrator":
            result.update(
                {
                    "can_be_edited": False,
                    "is_anonymous": False,
                    "can_manage_chat": True,
                    "can_delete_messages": True,
                    "can_manage_video_chats": True,
                    "can_restrict_members": True,
                    "can_promote_members": False,
                    "can_change_info": True,
                    "can_invite_users": True,
                    "can_post_stories": False,
                    "can_edit_stories": False,
                    "can_delete_stories": False,
                }
            )
        return result

    def _get_chat_administrators(self, fields: JsonObject) -> list[JsonObject]:
        chat_id = int(fields["chat_id"])
        self._required_chat(chat_id)
        admins = [
            self._get_chat_member({"chat_id": chat_id, "user_id": user_id})
            for (member_chat_id, user_id), status in self._member_statuses.items()
            if member_chat_id == chat_id and status in {"creator", "administrator"}
        ]
        return admins

    def _get_file(self, fields: JsonObject) -> JsonObject:
        file_id = str(fields["file_id"])
        if file_id not in self._files:
            raise ValueError(f"file {file_id!r} is not registered")
        path, data = self._files[file_id]
        return {
            "file_id": file_id,
            "file_unique_id": f"unique-{file_id}",
            "file_size": len(data),
            "file_path": path,
        }

    def _required_chat(self, chat_id: int) -> JsonObject:
        if chat_id not in self._chats:
            raise ValueError(f"chat {chat_id} is not registered")
        return self._chats[chat_id]

    def _required_user(self, user_id: int) -> JsonObject:
        if user_id not in self._users:
            raise ValueError(f"user {user_id} is not registered")
        return self._users[user_id]

    @staticmethod
    def _shallow_message(message: Mapping[str, Any]) -> JsonObject:
        return {
            key: value
            for key, value in message.items()
            if key not in {"reply_to_message", "pinned_message"}
        }

    @staticmethod
    def _rendered_text(fields: Mapping[str, Any]) -> str:
        text = str(fields["text"])
        if str(fields.get("parse_mode", "")).upper() != "HTML":
            return text
        parser = _TelegramHTMLTextParser()
        parser.feed(text)
        parser.close()
        return "".join(parser.parts)

    @staticmethod
    def _error(
        error_code: int,
        description: str,
        *,
        http_status: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> web.Response:
        payload: JsonObject = {
            "ok": False,
            "error_code": error_code,
            "description": description,
        }
        if parameters:
            payload["parameters"] = dict(parameters)
        return web.json_response(payload, status=http_status or error_code)


__all__ = [
    "BotAPIAttachment",
    "BotAPICall",
    "BotAPIFailure",
    "TelegramBotAPIServer",
]
