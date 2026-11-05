"""A live world for agent evals: Derp's real dispatcher, runtime and models against a fake Telegram.

Everything runs for real except Telegram, which ``tests.e2e.fake_api.FakeBotAPI`` stands in for: model
calls go to OpenRouter with the key from ``.env`` and spend real money. Each eval migrates a scratch
database afresh (``EVAL_DATABASE_URL``; by default ``derp_eval`` in the ``make db-test-up`` container,
created on first use). ``seed`` fills one synthetic group per scenario: facts a few days back, a day of
banter on top that pushes them out of the window, a cat photo, stickers, recent lines and a car photo
just before the ask. Nothing here touches real chats or people.
"""

import asyncio
import io
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram.types import Message as TgMessage
from alembic import command
from PIL import Image, ImageDraw
from sqlalchemy import Connection, make_url, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from derp.agent.models import AgentRun
from derp.config import Decisions, Settings
from derp.db.engine import Database
from derp.history.capture import index_sticker, store_message, upsert_chat, upsert_user
from derp.history.members import track_message
from derp.inference.models import ProviderCall
from derp.memory.models import Note
from derp.money.wallets import credit, wallet
from tests.conftest import alembic_config
from tests.e2e.fake_api import FakeBotAPI
from tests.e2e.harness import Conversation, Person, Room, Sent

REPO = Path(__file__).resolve().parents[2]
EVAL_URL = os.environ.get("EVAL_DATABASE_URL", "postgresql+asyncpg://derp:derp@localhost:5433/derp_eval")
BOT_ID = 777000111
TOKEN = f"{BOT_ID}:TEST_TOKEN_FOR_TESTING_ONLY_0000000"  # the fake Bot API's
NO_RUN_SECONDS = 150  # a message that started no run by then gets none (queued runs can take a while)
RUN_SECONDS = 240  # the longest a run may take before it counts as lost

JO = Person(5001, "Jo", "jo_eval", "ru")
ANYA = Person(5002, "Аня", "anya_eval", "ru")
BOB = Person(5003, "Bob", "bob_eval", "en")
VASYA = Person(5004, "Вася", "vasya_eval", "ru")
LENA = Person(5005, "Лена", None, "ru")
PEOPLE = (JO, ANYA, BOB, VASYA, LENA)

# (minutes ago, who, text, key): what scenarios ask about, a few days back
FACTS: list[tuple[float, Person, str, str | None]] = [
    (4400, VASYA, "кто в пятницу в бар?", None),
    (4398, ANYA, "я за! только не в Лося, там пиво разбавляют", None),
    (4396, BOB, "I'm in. What about that new place near the station?", None),
    (4390, LENA, "Хинкальная у вокзала норм, я там была, хачапури огонь", "khinkali"),
    (4385, VASYA, "ок, тогда хинкальная у вокзала, пятница 20:00", "decision"),
    (4380, ANYA, "Вася опять опоздает на час, как всегда 😂", None),
    (4379, VASYA, "это было один раз!!!", None),
    (4378, BOB, "it was three times Vasya", None),
    (2950, ANYA, "кто-нибудь смотрел Дюну 3?", None),
    (2948, BOB, "yeah, it's mid. the second one was better", None),
    (2940, JO, "Anya you have to watch it, the sandworm scene is insane", "jo_last"),
    (2930, LENA, "кстати, у Ани день рождения 14 ноября, не забудьте", "birthday"),
    (2929, VASYA, "я подарю ей кактус 🌵", None),
    (2928, ANYA, "Вася, я тебя убью", None),
    (2900, LENA, "Jo, ты вернула мне книгу? «Мастер и Маргарита» которая", "question_to_jo"),
    (2890, BOB, "@jo_eval are you coming on friday?", "mention_jo"),
    (2880, VASYA, "мой пароль от вайфая на даче: kartoshka2024, если что", None),
]
RU = [
    "ахахах", "ну да", "кто-нибудь видел мою зарядку?", "го в доту вечером", "я спать", "лол", "это база",
    "кринж", "а чё так дорого", "погода дичь", "кофе закончился", "завтра пара в 9, убейте", "я на созвоне",
    "щас буду", "скинь ссылку", "ты где", "норм", "согл", "жиза", "ору", "ну такое", "пойду поем",
    "у меня опять интернет отвалился", "кто смотрел вчера матч?", "бля, проспал", "ахах, точно",
    "не, я пас", "окей", "а кто знает хороший барбершоп?", "я в метро, плохо слышно", "сегодня дождь весь день",
    "мне лень", "это просто шедевр", "надо бы в отпуск", "кто-нибудь шарит за налоги?", "ну и жара",
]  # fmt: skip
EN = [
    "lol", "same", "brb", "anyone up for gaming tonight?", "this is so true", "I'm so tired", "coffee time",
    "lmao", "no way", "deadline tomorrow ugh", "nice", "bruh", "who took my charger", "gm", "gn everyone",
    "that's wild", "facts", "I'm starving", "meeting in 5", "ok boomer", "send pics", "hahaha true",
]  # fmt: skip
RECENT: list[tuple[float, Person, str, str | None]] = [
    (14, VASYA, "пацаны, я машину разбил 😭", "crash"),
    (13, ANYA, "живой хоть?", None),
    (12, VASYA, "живой, бампер только", None),
    (11, BOB, "rip bumper", None),
    (10, LENA, "Вася, ты как всегда", None),
    (8, BOB, "btw the deploy failed again on friday lol", "deploy"),
    (6, VASYA, "кто будет скидываться на подарок Ане?", None),
    (5, LENA, "я 500", None),
    (4, BOB, "1000 from me", None),
]
FILLER = 700  # banter lines over the last day: enough to push the facts out of the window


def settings(api_base: str) -> Settings:
    return Settings(
        _env_file=REPO / ".env",  # pyright: ignore[reportCallIssue]  # OpenRouter key; Telegram is the fake
        environment="test",
        telegram_bot_token=TOKEN,
        telegram_api_base=api_base,
        database_url=EVAL_URL,
        owner_ids=[1000],
        decisions=Decisions(enabled=False),
        logfire_token="",
        heartbeat_path=Path(tempfile.gettempdir()) / "derp-eval-poll",
    )


def _upgrade(connection: Connection) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def migrate() -> None:
    """The eval database with a fresh schema at head (the database is created on first use)."""
    url = make_url(EVAL_URL)
    server = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with server.connect() as connection:
        found = await connection.scalar(
            text("select 1 from pg_database where datname = :name"), {"name": url.database}
        )
        if not found:
            await connection.execute(text(f'create database "{url.database}"'))
    await server.dispose()
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.execute(text("drop schema if exists public cascade"))
        await connection.execute(text("create schema public"))
        await connection.commit()
        await connection.run_sync(_upgrade)
    await engine.dispose()


def picture(label: str, color: str = "skyblue") -> bytes:
    """A JPEG of a cartoon cat face with ``label`` written under it, for photos the models look at."""
    image = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(image)
    draw.ellipse((220, 120, 420, 320), fill="orange", outline="black", width=6)
    draw.polygon([(240, 150), (270, 60), (310, 130)], fill="orange", outline="black")
    draw.polygon([(330, 130), (370, 60), (400, 150)], fill="orange", outline="black")
    draw.ellipse((270, 190, 300, 220), fill="black")
    draw.ellipse((340, 190, 370, 220), fill="black")
    draw.text((20, 440), label, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


@dataclass
class Seeded:
    room: Room
    ids: dict[str, int] = field(default_factory=dict[str, int])  # message ids by key ("cat_photo", "crash"…)


@dataclass
class World:
    conv: Conversation
    api: FakeBotAPI
    db: Database
    files: int = 0

    @classmethod
    async def start(cls) -> World:
        await migrate()
        bot_user = {
            "id": BOT_ID, "is_bot": True, "first_name": "Derp", "username": "DerpRobot", "can_join_groups": True,
            "can_read_all_group_messages": True, "supports_inline_queries": True, "has_topics_enabled": False,
        }  # fmt: skip
        api = FakeBotAPI(token=TOKEN, bot_user=bot_user)
        await api.start()
        db = Database(EVAL_URL, pool_size=10)
        conv = Conversation(settings=settings(api.base_url), db=db, api=api)
        await conv.start()
        return cls(conv, api, db)

    async def stop(self) -> None:
        await self.conv.stop()
        await self.api.close()
        await self.db.close()

    async def ask(self, who: Person, room: Room, text_: str, *, reply_to: int | None = None) -> Sent:
        """A message to Derp, through polling like a real update."""
        return await self.conv.send(who, room, text_, reply_to=reply_to)

    def payload(self, room: Room, who: Person, minutes_ago: float, **content: Any) -> dict[str, Any]:
        return {
            "message_id": self.api.take_message_id(room.id),
            "date": int((datetime.now(UTC) - timedelta(minutes=minutes_ago)).timestamp()),
            "chat": room.payload(who),
            "from": who.payload(),
            **content,
        }

    def photo(self, label: str, color: str = "skyblue") -> list[dict[str, Any]]:
        """A photo the fake Bot API serves (its ``photo`` field)."""
        self.files += 1
        file_id = f"photo-{self.files}"
        blob = picture(label, color)
        self.api.register_file(file_id, blob)
        return [
            {
                "file_id": file_id,
                "file_unique_id": f"u-{file_id}",
                "width": 640,
                "height": 480,
                "file_size": len(blob),
            }
        ]

    async def store(self, payload: dict[str, Any]) -> int:
        """An earlier message stored as capture would have stored it (no update, no run)."""
        self.api.register_message(payload)
        message = TgMessage.model_validate(
            payload, context={"bot": self.conv.services.bot if self.conv.services else None}
        )
        async with self.db.transaction() as session:
            await upsert_chat(session, message.chat)
            if message.from_user is not None:
                await upsert_user(session, message.from_user)
            stored = await store_message(session, message, capture="ambient", bot_id=BOT_ID)
            await track_message(session, message, stored=stored)
            await index_sticker(session, message, BOT_ID)
        return int(payload["message_id"])

    async def fund_chat(self, room: Room, amount: int = 5000) -> None:
        async with self.db.transaction() as session:
            wallet_id = (await wallet(session, "chat", room.id)).id
            await credit(session, wallet_id, f"stars:eval:{room.id}", amount, "purchase")

    async def note(self, room: Room, text_: str, *, author: int) -> int:
        async with self.db.transaction() as session:
            note = Note(chat_id=room.id, text=text_, user_id=None, author_id=author, state="active")
            session.add(note)
            await session.flush()
            return note.id

    async def run_of(self, sent: Sent) -> AgentRun | None:
        """The run the message triggered, once finished; None if it started none."""
        chat_id = int(sent.message["chat"]["id"])
        started = time.monotonic()
        while time.monotonic() - started < RUN_SECONDS:
            async with self.db.session() as session:
                run = await session.scalar(
                    select(AgentRun)
                    .where(AgentRun.chat_id == chat_id, AgentRun.trigger_message_id == sent.message_id)
                    .order_by(AgentRun.id.desc())
                    .limit(1)
                )
            if run is not None and run.finished_at is not None:
                await asyncio.sleep(0.5)  # its deliveries land just after
                return run
            if run is None and time.monotonic() - started > NO_RUN_SECONDS:
                return None
            await asyncio.sleep(0.5)
        return None

    async def provider_calls(self, run_id: int) -> list[ProviderCall]:
        async with self.db.session() as session:
            return list(await session.scalars(select(ProviderCall).where(ProviderCall.run_id == run_id)))


async def seed(world: World, room: Room, *, admins: tuple[Person, ...] = (JO,), rng_seed: int = 7) -> Seeded:
    """The group with its history stored directly, members known to the fake Bot API, its wallet funded."""
    for person in PEOPLE:
        world.conv.meet(person, room, status="administrator" if person in admins else "member")
    world.api.profile_audios[LENA.id] = [
        {
            "file_id": "song-1",
            "file_unique_id": "u-song-1",
            "duration": 215,
            "performer": "Земфира",
            "title": "Хочешь?",
        }
    ]
    out = Seeded(room)
    for minutes, who, line, key in FACTS:
        message_id = await world.store(world.payload(room, who, minutes, text=line))
        if key:
            out.ids[key] = message_id
    cat = world.payload(room, BOB, 2800, caption="my cat judging me", photo=world.photo("my cat judging me"))
    out.ids["cat_photo"] = await world.store(cat)
    rng = random.Random(rng_seed)
    talkers = (ANYA, BOB, VASYA, LENA, VASYA, ANYA)  # Vasya and Anya talk the most
    for n in range(FILLER):
        who = rng.choice(talkers)
        await world.store(
            world.payload(room, who, 1400 - n * 1380 / FILLER, text=rng.choice(EN if who is BOB else RU))
        )
    for emoji in ("😂", "👍", "🔥"):
        world.files += 1
        file_id = f"sticker-{world.files}"
        world.api.register_file(file_id, b"RIFF0000WEBPVP8 ")
        sticker = {
            "file_id": file_id, "file_unique_id": f"u-{file_id}", "type": "regular", "width": 512, "height": 512,
            "is_animated": False, "is_video": False, "emoji": emoji, "set_name": "EvalPack",
        }  # fmt: skip
        await world.store(world.payload(room, ANYA, 20, sticker=sticker))
    for minutes, who, line, key in RECENT:
        message_id = await world.store(world.payload(room, who, minutes, text=line))
        if key:
            out.ids[key] = message_id
    car = world.payload(
        room, VASYA, 3, caption="вот, полюбуйтесь", photo=world.photo("Vasya's car bumper", "lightgray")
    )
    out.ids["car_photo"] = await world.store(car)
    await world.fund_chat(room)
    return out
