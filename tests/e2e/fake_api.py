"""Strict in-process Telegram Bot API for end-to-end tests.

Real HTTP and JSON envelopes over aiohttp. Every request is validated against
aiogram's method model; methods without an explicit handler, unknown fields
and unanswered callback queries fail the test at teardown.
"""

import asyncio
import contextlib
import inspect
import json
import typing
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import aiogram.methods
from aiogram.methods.base import TelegramMethod
from aiogram.types import Update
from aiohttp import web
from aiohttp.web_request import FileField

type JsonObject = dict[str, Any]

METHODS: dict[str, type[TelegramMethod[Any]]] = {
    cls.__api_method__: cls
    for cls in vars(aiogram.methods).values()
    if inspect.isclass(cls) and issubclass(cls, TelegramMethod) and hasattr(cls, "__api_method__")
}

# Methods whose Bot API result is a bare ``true`` and need no state here.
TRUE_METHODS = frozenset(
    {
        "sendChatAction",
        "setMyCommands",
        "deleteMyCommands",
        "setChatMenuButton",
        "setMyDefaultAdministratorRights",
        "sendRichMessageDraft",
        "sendMessageDraft",
        "answerPreCheckoutQuery",
        "answerInlineQuery",
        "refundStarPayment",
        "editUserStarSubscription",
        "pinChatMessage",
        "unpinChatMessage",
        "leaveChat",
    }
)


def rich_blocks(source: str) -> JsonObject:
    """A received ``RichMessage``: one paragraph block per source paragraph."""
    return {"blocks": [{"type": "paragraph", "text": p} for p in source.split("\n\n") if p]}


@dataclass(frozen=True, slots=True)
class Attachment:
    field: str
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Call:
    sequence: int
    method: str
    fields: Mapping[str, Any]
    attachments: tuple[Attachment, ...] = ()

    def __getitem__(self, name: str) -> Any:
        if name not in self.fields:
            raise AssertionError(f"{self.method} did not include {name!r}")
        return self.fields[name]


@dataclass(frozen=True, slots=True)
class Failure:
    error_code: int
    description: str
    parameters: Mapping[str, Any] | None = None


class FakeBotAPI:
    def __init__(self, *, token: str, bot_user: Mapping[str, Any], long_poll_cap: float = 0.2) -> None:
        self.token = token
        self.bot_user = dict(bot_user)
        self.long_poll_cap = long_poll_cap
        self.condition = asyncio.Condition()
        self.calls: list[Call] = []
        self.protocol_errors: list[str] = []
        self.failures: dict[str, deque[Failure]] = defaultdict(deque)
        self.updates: list[JsonObject] = []
        self.confirmed_offset = 0
        self.chats: dict[int, JsonObject] = {}
        self.users: dict[int, JsonObject] = {int(self.bot_user["id"]): self.bot_user}
        self.member_statuses: dict[tuple[int, int], str] = {}
        self.messages: dict[tuple[int, int], JsonObject] = {}
        self.outstanding_callbacks: set[str] = set()
        self.next_message_ids: dict[int, int] = defaultdict(int)
        self.files: dict[str, tuple[str, bytes]] = {}
        self.poll_ids = 0
        self.pinned: dict[int, int] = {}
        self.handlers: dict[str, Callable[[JsonObject], Any]] = {}
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    # lifecycle

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/bot{token}/{method}", self._handle_api)
        app.router.add_get("/file/bot{token}/{path:.*}", self._handle_file)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        host, port = self._runner.addresses[0][:2]
        self.base_url = f"http://{host}:{port}"

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def assert_clean(self) -> None:
        if self.protocol_errors:
            raise AssertionError("; ".join(self.protocol_errors))
        if self.outstanding_callbacks:
            raise AssertionError(f"unanswered callback queries: {sorted(self.outstanding_callbacks)}")

    # world setup

    def register_user(self, user: Mapping[str, Any]) -> None:
        self.users[int(user["id"])] = dict(user)

    def register_chat(self, chat: Mapping[str, Any]) -> None:
        self.chats[int(chat["id"])] = dict(chat)

    def set_member_status(self, *, chat_id: int, user_id: int, status: str) -> None:
        self.member_statuses[(chat_id, user_id)] = status

    def register_file(self, file_id: str, data: bytes) -> None:
        self.files[file_id] = (f"files/{file_id}", data)

    def register_message(self, message: Mapping[str, Any]) -> None:
        parsed = deepcopy(dict(message))
        key = (int(parsed["chat"]["id"]), int(parsed["message_id"]))
        self.messages[key] = parsed
        self.next_message_ids[key[0]] = max(self.next_message_ids[key[0]], key[1])

    def take_message_id(self, chat_id: int) -> int:
        self.next_message_ids[chat_id] += 1
        return self.next_message_ids[chat_id]

    def handle(self, method: str, handler: Callable[[JsonObject], Any]) -> None:
        """Serve a method with a test-specific responder."""
        self.handlers[method] = handler

    def fail_next(
        self, method: str, *, error_code: int, description: str, parameters: Mapping[str, Any] | None = None
    ) -> None:
        self.failures[method].append(Failure(error_code, description, parameters))

    def expect_callback(self, callback_id: str, *, chat_id: int, message_id: int, data: str) -> JsonObject:
        message = self.message(chat_id, message_id)
        rows = (message.get("reply_markup") or {}).get("inline_keyboard", [])
        visible = {button.get("callback_data") for row in rows for button in row}
        if data not in visible:
            raise AssertionError(f"callback data {data!r} is not on the live message")
        self.outstanding_callbacks.add(callback_id)
        return message

    async def push_update(self, update: Mapping[str, Any]) -> int:
        raw = dict(update)
        Update.model_validate(raw)
        async with self.condition:
            self.updates.append(raw)
            self.updates.sort(key=lambda item: int(item["update_id"]))
            self.condition.notify_all()
        return int(raw["update_id"])

    # observation

    def message(self, chat_id: int, message_id: int) -> JsonObject:
        try:
            return deepcopy(self.messages[(chat_id, message_id)])
        except KeyError:
            raise AssertionError(f"message {(chat_id, message_id)} is not live") from None

    def calls_of(self, method: str, after: int = 0) -> list[Call]:
        return [call for call in self.calls if call.method == method and call.sequence > after]

    async def wait_for(self, predicate: Callable[[], Any], timeout: float, what: str) -> Any:
        async with self.condition:
            try:
                await asyncio.wait_for(self.condition.wait_for(predicate), timeout)
            except TimeoutError:
                recent = ", ".join(f"{c.sequence}:{c.method}" for c in self.calls[-12:])
                raise AssertionError(
                    f"timed out waiting for {what}; recent calls: {recent or 'none'}"
                ) from None
            return predicate()

    async def wait_call(
        self,
        method: str,
        *,
        after: int = 0,
        where: Callable[[Call], bool] | None = None,
        timeout: float = 5,
    ) -> Call:
        def match() -> Call | None:
            return next(
                (c for c in self.calls_of(method, after) if where is None or where(c)),
                None,
            )

        return await self.wait_for(match, timeout, method)

    async def wait_confirmed(self, update_id: int, timeout: float = 5) -> None:
        await self.wait_for(lambda: self.confirmed_offset > update_id, timeout, f"offset > {update_id}")

    # HTTP

    async def _handle_api(self, request: web.Request) -> web.Response:
        if request.match_info["token"] != self.token:
            return self._error(401, "Unauthorized")
        method = request.match_info["method"]
        model = METHODS.get(method)
        if model is None:
            self.protocol_errors.append(f"unknown Bot API method {method!r}")
            return self._error(404, "Not Found: method not found")
        try:
            fields, attachments = await self._decode(request, model)
            model.model_validate(fields)
        except Exception as error:  # noqa: BLE001 - recorded as a protocol error
            self.protocol_errors.append(f"invalid {method}: {error}")
            return self._error(400, f"Bad Request: invalid {method}")
        async with self.condition:
            call = Call(len(self.calls) + 1, method, MappingProxyType(deepcopy(fields)), attachments)
            self.calls.append(call)
            self.condition.notify_all()
        if self.failures.get(method):
            failure = self.failures[method].popleft()
            return self._error(failure.error_code, failure.description, failure.parameters)
        try:
            result = await self._dispatch(method, fields)
        except Exception as error:  # noqa: BLE001 - recorded as a protocol error
            self.protocol_errors.append(f"failed to serve {method}: {error}")
            return self._error(400, f"Bad Request: {error}")
        async with self.condition:
            self.condition.notify_all()
        return web.json_response({"ok": True, "result": result})

    async def _handle_file(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["token"] != self.token:
            raise web.HTTPUnauthorized()
        path = request.match_info["path"]
        for file_path, data in self.files.values():
            if file_path == path:
                return web.Response(body=data, content_type="application/octet-stream")
        raise web.HTTPNotFound()

    async def _decode(
        self, request: web.Request, model: type[TelegramMethod[Any]]
    ) -> tuple[JsonObject, tuple[Attachment, ...]]:
        form = await request.post()
        fields: JsonObject = {}
        attachments: list[Attachment] = []
        for name, value in form.items():
            if isinstance(value, FileField):
                attachments.append(Attachment(name, value.filename, value.content_type, value.file.read()))
            elif _is_text_field(model, name):
                fields[name] = str(value)
            else:
                fields[name] = self._decode_value(str(value))
        unknown = set(fields) - set(model.model_fields)
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        return fields, tuple(attachments)

    @staticmethod
    def _decode_value(value: str) -> Any:
        stripped = value.strip()
        if stripped.startswith(("{", "[")) or stripped in {"true", "false", "null"}:
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        return value

    async def _dispatch(self, method: str, fields: JsonObject) -> Any:
        if method in self.handlers:
            result = self.handlers[method](fields)
            return await result if inspect.isawaitable(result) else result
        builtin = getattr(self, f"_m_{method}", None)
        if builtin is not None:
            result = builtin(fields)
            return await result if inspect.isawaitable(result) else result
        if method in TRUE_METHODS:
            return True
        raise RuntimeError(f"no fake implementation for {method}")

    # method implementations

    def _m_getMe(self, _: JsonObject) -> JsonObject:  # noqa: N802
        return self.bot_user

    async def _m_getUpdates(self, fields: JsonObject) -> list[JsonObject]:  # noqa: N802
        offset = int(fields.get("offset", 0))
        limit = int(fields.get("limit", 100))
        timeout = min(float(fields.get("timeout", 0)), self.long_poll_cap)
        allowed = fields.get("allowed_updates")
        allowed_types = set(allowed) if isinstance(allowed, list) else None
        async with self.condition:
            if offset:
                self.confirmed_offset = max(self.confirmed_offset, offset)
                self.updates = [u for u in self.updates if int(u["update_id"]) >= offset]
                self.condition.notify_all()

            def available() -> list[JsonObject]:
                return [
                    u
                    for u in self.updates
                    if int(u["update_id"]) >= offset
                    and (allowed_types is None or any(k in allowed_types for k in u if k != "update_id"))
                ][:limit]

            if not available() and timeout > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.condition.wait_for(lambda: bool(available())), timeout)
            for update in self.updates:
                if allowed_types is not None and not any(
                    k in allowed_types for k in update if k != "update_id"
                ):
                    self.protocol_errors.append(f"update {update['update_id']} is not in allowed_updates")
            return [dict(u) for u in available()]

    def _new_message(self, fields: JsonObject, **content: Any) -> JsonObject:
        chat_id = int(fields["chat_id"])
        if chat_id not in self.chats:
            raise ValueError(f"chat {chat_id} is not registered")
        message: JsonObject = {
            "message_id": self.take_message_id(chat_id),
            "date": int(datetime.now(UTC).timestamp()),
            "chat": self.chats[chat_id],
            "from": self.bot_user,
            **content,
        }
        if thread_id := fields.get("message_thread_id"):
            message["message_thread_id"] = int(thread_id)
            message["is_topic_message"] = True
        if isinstance(markup := fields.get("reply_markup"), Mapping) and "inline_keyboard" in markup:
            message["reply_markup"] = markup
        reply = fields.get("reply_parameters")
        if isinstance(reply, Mapping):
            target = self.messages.get((chat_id, int(reply["message_id"])))
            if target is not None:
                message["reply_to_message"] = {
                    k: v for k, v in target.items() if k not in {"reply_to_message", "pinned_message"}
                }
            elif not reply.get("allow_sending_without_reply"):
                raise ValueError("message to be replied not found")
        self.register_message(message)
        return message

    def _m_sendMessage(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(fields, text=str(fields["text"]))

    def _m_sendRichMessage(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        rich = fields["rich_message"]  # Telegram returns the parsed blocks, never the source text
        return self._new_message(
            fields, rich_message=rich_blocks(str(rich.get("markdown") or rich.get("html") or ""))
        )

    def _m_sendRichMessageDraft(self, fields: JsonObject) -> bool:  # noqa: N802
        chat = self.chats.get(int(fields["chat_id"]), {})
        if chat.get("type") != "private":
            raise ValueError("drafts can be sent only to private chats")
        if int(fields["draft_id"]) == 0:
            raise ValueError("draft_id must be non-zero")
        return True

    def _m_sendChatAction(self, fields: JsonObject) -> bool:  # noqa: N802
        return True

    def _m_sendInvoice(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        if fields.get("currency") != "XTR" or "provider_token" in fields:
            raise ValueError("Stars invoices use XTR and no provider token")
        prices = fields["prices"]
        invoice = {
            "title": fields["title"],
            "description": fields["description"],
            "start_parameter": "",
            "currency": "XTR",
            "total_amount": sum(int(p["amount"]) for p in prices),
        }
        return self._new_message(fields, invoice=invoice)

    def _m_sendDice(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(fields, dice={"emoji": fields.get("emoji", "🎲"), "value": 4})

    def _m_sendPoll(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        self.poll_ids += 1
        options = [
            {"text": o["text"], "voter_count": 0, "persistent_id": f"o{i}"}
            for i, o in enumerate(fields["options"])
        ]
        poll: JsonObject = {
            "id": f"poll-{self.poll_ids}",
            "question": str(fields["question"]),
            "options": options,
            "total_voter_count": 0,
            "is_closed": False,
            "is_anonymous": str(fields.get("is_anonymous", "true")).lower() == "true",
            "type": str(fields.get("type", "regular")),
            "allows_multiple_answers": str(fields.get("allows_multiple_answers", "false")).lower() == "true",
            "allows_revoting": True,
            "members_only": False,
        }
        if correct := fields.get("correct_option_ids"):
            poll["correct_option_ids"] = list(correct)
        return self._new_message(fields, poll=poll)

    def _m_sendSticker(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        file_id = str(fields["sticker"])
        sticker = {
            "file_id": file_id, "file_unique_id": f"u-{file_id}", "type": "regular", "width": 512, "height": 512,
            "is_animated": False, "is_video": False,
        }  # fmt: skip
        return self._new_message(fields, sticker=sticker)

    def _m_pinChatMessage(self, fields: JsonObject) -> bool:  # noqa: N802
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        if key not in self.messages:
            raise ValueError(f"message {key} does not exist")
        self.pinned[key[0]] = key[1]
        return True

    def _m_editMessageText(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        message = self.messages.get(key)
        if message is None:
            raise ValueError(f"message {key} does not exist")
        rich = fields.get("rich_message")
        if isinstance(rich, Mapping):
            message.pop("text", None)
            message["rich_message"] = rich_blocks(str(rich.get("markdown")))
        else:
            message["text"] = str(fields["text"])
        message["edit_date"] = int(datetime.now(UTC).timestamp())
        if markup := fields.get("reply_markup"):
            message["reply_markup"] = markup
        else:
            message.pop("reply_markup", None)
        return dict(message)

    def _m_editMessageReplyMarkup(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        message = self.messages[key]
        if markup := fields.get("reply_markup"):
            message["reply_markup"] = markup
        else:
            message.pop("reply_markup", None)
        return dict(message)

    def _m_deleteMessage(self, fields: JsonObject) -> bool:  # noqa: N802
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        if self.messages.pop(key, None) is None:
            raise ValueError(f"message {key} does not exist")
        return True

    def _m_setMessageReaction(self, fields: JsonObject) -> bool:  # noqa: N802
        key = (int(fields["chat_id"]), int(fields["message_id"]))
        if key not in self.messages:
            raise ValueError(f"message {key} does not exist")
        self.messages[key]["_bot_reaction"] = fields.get("reaction") or []
        return True

    def _m_answerCallbackQuery(self, fields: JsonObject) -> bool:  # noqa: N802
        callback_id = str(fields["callback_query_id"])
        if callback_id not in self.outstanding_callbacks:
            raise ValueError(f"callback query {callback_id!r} is not outstanding")
        self.outstanding_callbacks.remove(callback_id)
        return True

    def _member(self, chat_id: int, user_id: int) -> JsonObject:
        status = self.member_statuses.get((chat_id, user_id), "member")
        result: JsonObject = {"status": status, "user": self.users[user_id]}
        if status == "creator":
            result["is_anonymous"] = False
        elif status == "administrator":
            result |= {
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
                "can_send_welcome_messages": False,
            }
        return result

    def _m_getChatMember(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._member(int(fields["chat_id"]), int(fields["user_id"]))

    def _m_getChatAdministrators(self, fields: JsonObject) -> list[JsonObject]:  # noqa: N802
        chat_id = int(fields["chat_id"])
        return [
            self._member(chat_id, user_id)
            for (cid, user_id), status in self.member_statuses.items()
            if cid == chat_id and status in {"creator", "administrator"}
        ]

    def _m_getChat(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        chat = dict(self.chats[int(fields["chat_id"])])
        return {"accent_color_id": 0, "max_reaction_count": 11, "accepted_gift_types": _NO_GIFTS, **chat}

    def _m_getChatMemberCount(self, fields: JsonObject) -> int:  # noqa: N802
        chat_id = int(fields["chat_id"])
        return sum(
            1
            for (cid, _), status in self.member_statuses.items()
            if cid == chat_id and status not in {"left", "kicked"}
        )

    def _m_getFile(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        file_id = str(fields["file_id"])
        path, data = self.files[file_id]
        return {
            "file_id": file_id,
            "file_unique_id": f"u-{file_id}",
            "file_size": len(data),
            "file_path": path,
        }

    @staticmethod
    def _error(code: int, description: str, parameters: Mapping[str, Any] | None = None) -> web.Response:
        payload: JsonObject = {"ok": False, "error_code": code, "description": description}
        if parameters:
            payload["parameters"] = dict(parameters)
        return web.json_response(payload, status=code)


def _is_text_field(model: type[TelegramMethod[Any]], name: str) -> bool:
    """String-typed fields are sent verbatim; only structured fields are JSON-encoded."""
    field = model.model_fields.get(name)
    if field is None:
        return False
    members = typing.get_args(field.annotation) or (field.annotation,)
    return all(
        member in (str, type(None)) or getattr(member, "__name__", "") == "Default" for member in members
    )


_NO_GIFTS = {
    "unlimited_gifts": False,
    "limited_gifts": False,
    "unique_gifts": False,
    "premium_subscription": False,
    "gifts_from_channels": False,
}
