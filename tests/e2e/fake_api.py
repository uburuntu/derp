"""Strict in-process Telegram Bot API for end-to-end tests.

Real HTTP and JSON envelopes over aiohttp. Every request is validated against
aiogram's method model; methods without an explicit handler, unknown fields
and unanswered callback queries fail the test at teardown.
"""

import asyncio
import contextlib
import inspect
import json
import time
import typing
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

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

EPHEMERAL_SECONDS = 15  # an ephemeral message must answer its press or command within this

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


class Refusal(Exception):  # noqa: N818 - Telegram's answer, not a fake failure
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code, self.description = code, description


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
        self.star_transactions: list[JsonObject] = []  # what getStarTransactions pages through
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
        self.inline_results: dict[str, bool] = {}  # result id → has a keyboard
        self.ephemerals: dict[tuple[int, int], JsonObject] = {}  # (chat, ephemeral id) → message
        self.ephemeral_commands: dict[tuple[int, int], int] = {}  # (chat, ephemeral id) → when it was sent
        # callback query id → (presser, pressed an ephemeral message, when): ephemeral answers to presses
        self.presses: dict[str, tuple[int, bool, float]] = {}
        self.invoice_links: dict[str, JsonObject] = {}  # createInvoiceLink URL → its fields
        self.inline_messages: dict[str, JsonObject] = {}  # inline_message_id → last edit
        self.pinned: dict[int, int] = {}
        self.profile: dict[
            tuple[str, str], str
        ] = {}  # (name | description | short_description, language) → text
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
        if isinstance(query := raw.get("callback_query"), Mapping):
            pressed_ephemeral = bool((query.get("message") or {}).get("ephemeral_message_id"))
            self.presses[str(query["id"])] = (int(query["from"]["id"]), pressed_ephemeral, time.monotonic())
        if isinstance(message := raw.get("message"), Mapping) and message.get("ephemeral_message_id"):
            key = (int(message["chat"]["id"]), int(message["ephemeral_message_id"]))
            self.ephemeral_commands[key] = int(message["date"])
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
        except Refusal as refusal:  # what Telegram itself would answer
            return self._error(refusal.code, refusal.description)
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

    def _profile(self, field: str, fields: JsonObject, value: str | None = None) -> JsonObject | bool:
        key = (field, str(fields.get("language_code") or ""))
        if value is None:
            return {field: self.profile.get(key, "")}
        self.profile[key] = value
        return True

    def _m_getMyName(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        return self._profile("name", fields)

    def _m_setMyName(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        return self._profile("name", fields, str(fields.get("name") or ""))

    def _m_getMyDescription(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        return self._profile("description", fields)

    def _m_setMyDescription(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        return self._profile("description", fields, str(fields.get("description") or ""))

    def _m_getMyShortDescription(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        return self._profile("short_description", fields)

    def _m_setMyShortDescription(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        return self._profile("short_description", fields, str(fields.get("short_description") or ""))

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
        if chat_id > 0 and chat_id not in self.chats and chat_id in self.users:
            raise Refusal(403, "Forbidden: bot can't initiate conversation with a user")
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
        ephemeral = fields.get("ephemeral_message_parameters")
        if (
            isinstance(reply, Mapping)
            and "ephemeral_message_id" in reply
            and not isinstance(ephemeral, Mapping)
        ):
            raise ValueError("a reply to an ephemeral message must itself be ephemeral")
        if isinstance(ephemeral, Mapping):  # seen only by the receiver; never a regular chat message
            receiver = self.users.get(int(ephemeral["receiver_user_id"]))
            if receiver is None:
                raise ValueError("ephemeral receiver is not a known user")
            if (query := ephemeral.get("callback_query_id")) is not None:
                press = self.presses.get(str(query))
                if press is None:
                    raise ValueError(f"callback query {query!r} was never received")
                presser, from_ephemeral, at = press
                if presser != int(receiver["id"]):
                    raise ValueError("an ephemeral answer to a press goes to the presser")
                if from_ephemeral and ephemeral.get("replace_callback_query_message"):
                    raise ValueError("presses on ephemeral messages can't be replaced, only edited")
                if time.monotonic() - at > EPHEMERAL_SECONDS:
                    raise Refusal(400, "Bad Request: query is too old")
            elif isinstance(reply, Mapping) and (answered := reply.get("ephemeral_message_id")):
                sent = self.ephemeral_commands.get((chat_id, int(answered)))
                if sent is None:
                    raise ValueError(f"ephemeral message {answered!r} was never received")
                if datetime.now(UTC).timestamp() - sent > EPHEMERAL_SECONDS:
                    raise Refusal(400, "Bad Request: REPLY_TO_INVALID")
            elif receiver.get("is_bot") or self.member_statuses.get(
                (chat_id, int(self.bot_user["id"]))
            ) not in {"creator", "administrator"}:  # the wording here is the fake's own
                raise Refusal(400, "Bad Request: ephemeral messages need a reply, a press or admin rights")
            message.update(message_id=0, ephemeral_message_id=len(self.calls), receiver_user=receiver)
            self.ephemerals[(chat_id, int(message["ephemeral_message_id"]))] = message
            return message
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

    def _m_getStarTransactions(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        offset, limit = int(fields.get("offset", 0)), int(fields.get("limit", 100))
        return {"transactions": self.star_transactions[offset : offset + limit]}

    def _m_createInvoiceLink(self, fields: JsonObject) -> str:  # noqa: N802
        period = fields.get("subscription_period")
        amount = sum(int(p["amount"]) for p in fields["prices"])
        if period is not None and (
            int(period) != 2592000 or fields.get("currency") != "XTR" or amount > 10000
        ):
            raise ValueError("subscriptions are 30-day XTR invoices of at most 10000 Stars")
        url = f"https://t.me/$invoice-{len(self.calls)}"
        self.invoice_links[url] = dict(fields)
        return url

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

    def _m_answerInlineQuery(self, fields: JsonObject) -> bool:  # noqa: N802
        results = fields.get("results") or []
        if not isinstance(results, list):
            raise TypeError("results must be a list")
        for result in cast(list[JsonObject], results):
            markup = result.get("reply_markup")
            self.inline_results[str(result["id"])] = bool(markup)
        return True

    def send_inline_result(self, result_id: str) -> str:
        """The user picked a result: Telegram posts it and reports an inline_message_id only if it has a keyboard."""
        if not self.inline_results.get(result_id):
            raise AssertionError("results without a keyboard report no inline_message_id")
        inline_id = f"inline-{len(self.inline_messages) + 1}"
        self.inline_messages[inline_id] = {}
        return inline_id

    def _m_answerGuestQuery(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        if not isinstance(fields.get("result"), Mapping):
            raise TypeError("result must be one InlineQueryResult")
        return {"inline_message_id": f"guest-{fields['guest_query_id']}"}

    def _m_sendPhoto(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        unique = f"photo-{len(self.calls)}"
        sizes = [{"file_id": f"f-{unique}", "file_unique_id": unique, "width": 1024, "height": 1024}]
        if uploads := self.calls[-1].attachments:  # like Telegram, the sent photo can be downloaded again
            self.register_file(f"f-{unique}", uploads[0].data)
        return self._new_message(fields, photo=sizes)

    def _uploaded(self, kind: str, **extra: Any) -> JsonObject:
        unique = f"{kind}-{len(self.calls)}"
        return {"file_id": f"f-{unique}", "file_unique_id": unique, **extra}

    def _m_sendVideo(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(fields, video=self._uploaded("video", width=640, height=360, duration=3))

    def _m_sendAnimation(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(
            fields, animation=self._uploaded("animation", width=640, height=360, duration=3)
        )

    def _m_sendVideoNote(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(fields, video_note=self._uploaded("video_note", length=384, duration=3))

    def _m_sendVoice(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(fields, voice=self._uploaded("voice", duration=2))

    def _m_sendDocument(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        return self._new_message(fields, document=self._uploaded("document", file_name="file"))

    def _m_getUserProfilePhotos(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        """One photo per user whose ``profile-<user id>`` file is registered."""
        file_id = f"profile-{int(fields['user_id'])}"
        if file_id not in self.files:
            return {"total_count": 0, "photos": []}
        size = {"file_id": file_id, "file_unique_id": f"u-{file_id}", "width": 640, "height": 640}
        return {"total_count": 1, "photos": [[size]]}

    def _m_sendSticker(self, fields: JsonObject) -> JsonObject:  # noqa: N802
        file_id = f"f-sticker-{len(self.calls)}" if self.calls[-1].attachments else str(fields["sticker"])
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

    def _m_editMessageText(self, fields: JsonObject) -> JsonObject | bool:  # noqa: N802
        if inline_id := fields.get("inline_message_id"):  # inline messages: Telegram returns True
            if str(inline_id) not in self.inline_messages:
                raise ValueError(f"inline message {inline_id} does not exist")
            self.inline_messages[str(inline_id)] = dict(fields)
            return True
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

    def _m_editEphemeralMessageText(self, fields: JsonObject) -> bool:  # noqa: N802
        key = (int(fields["chat_id"]), int(fields["ephemeral_message_id"]))
        message = self.ephemerals.get(key)
        if message is None or int(message["receiver_user"]["id"]) != int(fields["receiver_user_id"]):
            raise ValueError(f"ephemeral message {key} was not sent to that user")
        if isinstance(rich := fields.get("rich_message"), Mapping):
            message["rich_markdown"] = rich.get("markdown")
        if isinstance(markup := fields.get("reply_markup"), Mapping):
            message["reply_markup"] = markup
        else:
            message.pop("reply_markup", None)
        return True

    def expect_ephemeral_callback(
        self, callback_id: str, *, chat_id: int, ephemeral_id: int, data: str
    ) -> JsonObject:
        message = self.ephemerals[(chat_id, ephemeral_id)]
        rows = (message.get("reply_markup") or {}).get("inline_keyboard", [])
        if data not in {button.get("callback_data") for row in rows for button in row}:
            raise AssertionError(f"callback data {data!r} is not on the live ephemeral message")
        self.outstanding_callbacks.add(callback_id)
        return {k: v for k, v in message.items() if k not in {"rich_markdown"}}

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
        """The chat as registered, with a photo when a ``chatphoto-<chat id>`` file is registered."""
        chat_id = int(fields["chat_id"])
        chat = dict(self.chats[chat_id])
        if (file_id := f"chatphoto-{chat_id}") in self.files:
            chat["photo"] = {
                "small_file_id": file_id, "small_file_unique_id": f"u-{file_id}",
                "big_file_id": file_id, "big_file_unique_id": f"u-{file_id}",
            }  # fmt: skip
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
