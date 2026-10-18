import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Message as TgMessage
from sqlalchemy import select

from derp.agent.decisions import Decider, Verdict
from derp.agent.invocation import TriggerKind, detect_trigger
from derp.agent.media import Referenced
from derp.config import Decisions, Settings
from derp.db.engine import Database
from derp.history.models import Chat, Message, MessageMedia, User
from derp.inference.api import Decided, OpenRouterAPI, OpenRouterError
from derp.inference.models import ProviderCall
from derp.inference.pins import PinStore

CHAT = -700
UTC_TZ = ZoneInfo("UTC")
START = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
MENTIONS = json.loads((Path(__file__).parent / "name_mentions.json").read_text())


def tg(message_id: int, text: str) -> TgMessage:
    return TgMessage.model_validate(
        {
            "message_id": message_id,
            "date": START + timedelta(minutes=message_id),
            "chat": {"id": CHAT, "type": "supergroup", "title": "Yard"},
            "from": {"id": 1, "is_bot": False, "first_name": "Ann"},
            "text": text,
        }
    )


@pytest.mark.parametrize("case", MENTIONS, ids=lambda case: case["text"][:30])
def test_the_synthetic_mentions_are_all_name_only_triggers(case: dict[str, Any]) -> None:
    """The live probe's set (tests/live) only holds messages the name pattern actually invokes Derp for."""
    trigger = detect_trigger(tg(5, case["text"]), bot_id=42, bot_username="DerpRobot")
    assert trigger is not None and trigger.kind is TriggerKind.NAME


def decider(api: object = None, db: object = None, **settings: Any) -> Decider:
    return Decider(cast(Database, db), cast(OpenRouterAPI, api), cast(PinStore, None), Decisions(**settings))


def test_answers_become_a_verdict_at_the_thresholds() -> None:
    read = decider().read
    assert read({"addressed": {"noul": 0.31}})[0] == Verdict(addressed=True)
    assert read({"addressed": {"noul": 0.12}})[0] == Verdict(addressed=False)
    assert read({"images": {"noul": 0.03}})[0] == Verdict(images=False)
    assert read({"images": {"noul": 0.86}})[0] == Verdict()
    # Probabilities seen live: a multi-step word problem, a proof, a familiar explanation.
    word_problem = {"probabilities": {"0": 0.07, "1": 0.87, "2": 0.06}}
    proof = {"probabilities": {"0": 0.01, "1": 0.01, "2": 0.98}}
    explanation = {"probabilities": {"0": 0.78, "1": 0.22, "2": 0}}
    assert read({"effort": word_problem})[0].effort == "medium"
    assert read({"effort": proof})[0].effort == "high"
    verdict, seen = read({"effort": explanation, "addressed": {"noul": 0.9}})
    assert verdict == Verdict() and seen == {
        "addressed": 0.9,
        "effort_some": 0.22,
        "effort_deep": 0,
        "effort": "pin",
    }


@dataclass
class FakeDecisions:
    answers: dict[str, Any] = field(default_factory=dict[str, Any])
    error: Exception | None = None
    delay: float = 0
    asked: list[tuple[Any, dict[str, Any]]] = field(default_factory=list[tuple[Any, dict[str, Any]]])

    async def decide(self, model: str, state: Any, questions: dict[str, Any], *, zdr: bool = True) -> Decided:
        assert model == "typesafe/jev-1.14" and zdr
        self.asked.append((state, questions))
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return Decided(
            {name: self.answers[name] for name in questions}, "typesafe/jev-1.14-x", Decimal("0.00002"), 400
        )


async def seed(database: Database) -> None:
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="Yard"))
        session.add_all([User(id=1, first_name="Ann"), User(id=2, first_name="Bob")])
        await session.flush()
        rows = [
            (1, 2, "old news", None),
            (2, 2, "elsewhere", 77),  # another forum topic
            (3, 1, "", None),
            (4, 2, "nice pic", None),
            (5, 2, "who's coming tonight?", None),
            (6, 1, "derp, what breed is this?", None),
        ]
        for message_id, user_id, text, thread in rows:
            session.add(
                Message(
                    chat_id=CHAT, message_id=message_id, user_id=user_id, thread_id=thread,
                    date=START + timedelta(minutes=message_id), kind="photo" if message_id == 3 else "text",
                    text=text or None, capture="ambient", payload={},
                )
            )  # fmt: skip
        await session.flush()
        session.add(
            MessageMedia(
                chat_id=CHAT, message_id=3, idx=0, type="photo", file_id="f", bot_id=42, file_unique_id="u"
            )
        )


async def pins(database: Database, settings: Settings) -> PinStore:
    store = PinStore(database, settings)
    await store.seed()
    return store


@pytest.mark.db
async def test_one_call_asks_what_applies_on_short_state_and_is_ledgered(
    database: Database, settings: Settings
) -> None:
    await seed(database)
    api = FakeDecisions(
        {
            "addressed": {"type": "noul", "noul": 0.95},
            "images": {"type": "noul", "noul": 0.97},
            "effort": {"type": "score", "score": 0, "probabilities": {"0": 1, "1": 0, "2": 0}},
        }
    )
    deciding = Decider(database, cast(OpenRouterAPI, api), await pins(database, settings), Decisions())
    async with database.session() as session:
        photo = await session.get(MessageMedia, (CHAT, 3, 0))
    images = Referenced([photo], fallback=True) if photo else Referenced([])
    verdict = await deciding.decide(
        tg(6, "derp, what breed is this?"), bot_id=42, tz=UTC_TZ, named=True, images=images
    )
    assert verdict == Verdict()
    ((state, questions),) = api.asked
    assert list(questions) == ["addressed", "images", "effort"]
    assert [line.split(" ", 2)[2] for line in state["previous_messages"]] == [
        "Ann: [photo]",
        "Bob: nice pic",
        "Bob: who's coming tonight?",
    ]  # the last three in the trigger's topic
    assert state["images_from"] == ["[09:03] #3 Ann: [photo]"]
    assert state["new_message"] == "[09:06] #6 Ann: derp, what breed is this?"
    async with database.session() as session:
        call = await session.scalar(select(ProviderCall))
    assert call is not None and (call.kind, call.funding, call.role, call.run_id) == (
        "decision",
        "creator",
        "decision",
        None,
    )
    assert call.cost_usd == Decimal("0.00002") and call.input_tokens == 400 and call.chat_id == CHAT


@pytest.mark.db
async def test_effort_alone_is_asked_only_for_paid_turns(database: Database, settings: Settings) -> None:
    await seed(database)
    api = FakeDecisions({"effort": {"type": "score", "probabilities": {"0": 0.01, "1": 0.02, "2": 0.97}}})
    deciding = Decider(database, cast(OpenRouterAPI, api), await pins(database, settings), Decisions())
    message = tg(5, "@DerpRobot prove sqrt(2) is irrational")
    assert await deciding.decide(message, bot_id=42, tz=UTC_TZ, images=Referenced([])) == Verdict()
    assert api.asked == []  # nothing to ask on an unpaid mention without fallback images
    assert (await deciding.decide(message, bot_id=42, tz=UTC_TZ, paid=True)).effort == "high"
    assert list(api.asked[0][1]) == ["effort"]


@pytest.mark.db
@pytest.mark.parametrize(
    "api",
    [
        FakeDecisions(error=OpenRouterError(503, "down")),
        FakeDecisions(delay=1),  # past the timeout
        FakeDecisions({"addressed": {"type": "noul"}, "effort": {}}),  # unreadable answers
    ],
)
async def test_failures_decide_as_if_nothing_was_asked(
    database: Database, settings: Settings, api: FakeDecisions
) -> None:
    await seed(database)
    deciding = Decider(
        database, cast(OpenRouterAPI, api), await pins(database, settings), Decisions(timeout_seconds=0.05)
    )
    assert await deciding.decide(tg(6, "derp moment"), bot_id=42, tz=UTC_TZ, named=True) == Verdict()
    assert len(api.asked) == 1


async def test_disabled_decisions_ask_nothing() -> None:
    api = FakeDecisions()
    verdict = await decider(api, enabled=False).decide(
        tg(6, "derp lol"), bot_id=42, tz=UTC_TZ, named=True, paid=True
    )
    assert verdict == Verdict() and api.asked == []
