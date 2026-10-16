"""Conversation-level harness: real polling against the fake Bot API, real PostgreSQL."""

import asyncio
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Dispatcher

from derp.app import Services, build_dispatcher
from derp.config import Settings
from derp.db.engine import Database
from derp.telegram.bot import create_bot
from derp.telegram.updates import ALLOWED_UPDATES
from tests.e2e.fake_api import Call, FakeBotAPI


@dataclass(frozen=True, slots=True)
class Derived:
    """An override computed from the freshly created services (e.g. a component wired to a fake)."""

    build: Callable[[Services], Any]


@dataclass(frozen=True, slots=True)
class Person:
    id: int
    first_name: str
    username: str | None = None
    language_code: str = "en"
    is_bot: bool = False

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "is_bot": self.is_bot, "first_name": self.first_name}
        if self.username:
            data["username"] = self.username
        if self.language_code:
            data["language_code"] = self.language_code
        return data


@dataclass(frozen=True, slots=True)
class Room:
    id: int
    type: str = "supergroup"
    title: str | None = None
    is_forum: bool = False

    @classmethod
    def private(cls, person: Person) -> Room:
        return cls(person.id, "private")

    def payload(self, person: Person | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.type == "private":
            data["first_name"] = person.first_name if person else "User"
        else:
            data["title"] = self.title or "Test group"
            if self.is_forum:
                data["is_forum"] = True
        return data


@dataclass(frozen=True, slots=True)
class Sent:
    update_id: int
    message: Mapping[str, Any]
    after: int  # call sequence before the update was pushed

    @property
    def message_id(self) -> int:
        return int(self.message["message_id"])


@dataclass(slots=True)
class Conversation:
    settings: Settings
    db: Database
    api: FakeBotAPI
    services: Services | None = None
    dispatcher: Dispatcher | None = None
    task: asyncio.Task[None] | None = None
    overrides: dict[str, Any] = field(default_factory=dict[str, Any])
    _update_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def start(self) -> None:
        bot = create_bot(self.settings)
        self.services = Services.create(self.settings, self.db, bot)
        for name, value in self.overrides.items():
            setattr(self.services, name, value.build(self.services) if isinstance(value, Derived) else value)
        self.dispatcher = build_dispatcher(self.services)
        polls = len(self.api.calls_of("getUpdates"))
        self.task = asyncio.create_task(
            self.dispatcher.start_polling(
                bot,
                allowed_updates=list(ALLOWED_UPDATES),
                handle_signals=False,
                handle_as_tasks=False,  # handlers finish before the next poll confirms the offset
                polling_timeout=1,
            )
        )
        await self.api.wait_for(lambda: len(self.api.calls_of("getUpdates")) > polls, 5, "first poll")

    async def stop(self) -> None:
        if self.dispatcher is not None and self.task is not None and not self.task.done():
            await self.dispatcher.stop_polling()
            await asyncio.wait_for(self.task, 5)
        if self.services is not None:
            await self.services.bot.session.close()
            await self.services.api.close()
        self.task = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    @property
    def seq(self) -> int:
        return len(self.api.calls)

    def meet(self, person: Person, room: Room | None = None, *, status: str = "member") -> None:
        self.api.register_user(person.payload())
        room = room or Room.private(person)
        self.api.register_chat(room.payload(person))
        if room.type != "private":
            self.api.set_member_status(chat_id=room.id, user_id=person.id, status=status)

    async def send(
        self,
        person: Person,
        room: Room | None = None,
        text: str | None = None,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
        entities: Sequence[Mapping[str, Any]] = (),
        **content: Any,
    ) -> Sent:
        room = room or Room.private(person)
        if room.id not in self.api.chats or person.id not in self.api.users:
            self.meet(person, room)
        message: dict[str, Any] = {
            "message_id": self.api.take_message_id(room.id),
            "date": int(datetime.now(UTC).timestamp()),
            "chat": room.payload(person),
            "from": person.payload(),
            **content,
        }
        if text is not None:
            message["text"] = text
        if entities:
            message["entities"] = list(entities)
        elif text and text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if thread_id is not None:
            message["message_thread_id"] = thread_id
            message["is_topic_message"] = True
        if reply_to is not None:
            message["reply_to_message"] = self.api.message(room.id, reply_to)
        self.api.register_message(message)
        after = self.seq
        update_id = await self.api.push_update({"update_id": next(self._update_ids), "message": message})
        return Sent(update_id, message, after)

    async def push(self, kind: str, payload: Mapping[str, Any]) -> int:
        return await self.api.push_update({"update_id": next(self._update_ids), kind: dict(payload)})

    async def expect_reply(self, sent: Sent, method: str = "sendRichMessage", timeout: float = 5) -> Call:
        """The first message sent in reply to ``sent`` (notices posted without a reply are skipped)."""
        chat_id = int(sent.message["chat"]["id"])

        def replies(call: Call) -> bool:
            target = call.fields.get("reply_parameters") or {}
            return int(call["chat_id"]) == chat_id and int(target.get("message_id", 0)) == sent.message_id

        return await self.api.wait_call(method, after=sent.after, where=replies, timeout=timeout)

    async def press(self, person: Person, chat_id: int, message_id: int, data: str) -> None:
        """Press an inline button with callback ``data`` on one of Derp's messages and wait for the handler."""
        query_id = f"q{len(self.api.calls)}"
        message = self.api.expect_callback(query_id, chat_id=chat_id, message_id=message_id, data=data)
        update_id = await self.push(
            "callback_query",
            {
                "id": query_id,
                "from": person.payload(),
                "chat_instance": "c",
                "message": message,
                "data": data,
            },
        )
        await self.settle(update_id)

    async def settle(self, sent: Sent | int, timeout: float = 5) -> None:
        """Wait until the update's handler finished (the next poll confirms its offset)."""
        update_id = sent.update_id if isinstance(sent, Sent) else sent
        await self.api.wait_confirmed(update_id, timeout)
