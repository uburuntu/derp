"""Jev's decisions before a run: passing mentions of Derp's name are ignored, unneeded follow-up photos are left
out, and a hard paid question gets a deeper reasoning effort."""

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from derp.agent.models import AgentRun
from derp.config import Decisions
from derp.inference.api import Decided
from derp.inference.models import ProviderCall
from derp.money.wallets import credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel
from tests.e2e.test_follow_up_media import sees
from tests.e2e.test_media import PNG, photo

FAY = Person(621, "Fay")
GUS = Person(622, "Gus")
PORCH = Room(-1062, title="Porch")

type Answers = Callable[[str], dict[str, Any]]  # the new message's line → answers by question


@dataclass
class FakeJev:
    """Stands in for OpenRouterAPI's Decisions call."""

    answers: Answers
    asked: list[tuple[dict[str, Any], list[str]]] = field(
        default_factory=list[tuple[dict[str, Any], list[str]]]
    )

    async def decide(
        self, model: str, state: dict[str, Any], questions: dict[str, Any], **_: object
    ) -> Decided:
        self.asked.append((state, list(questions)))
        given = self.answers(state["new_message"])
        return Decided({name: given[name] for name in questions}, model, Decimal("0.00002"), 400)

    async def close(self) -> None:
        return None


def noul(p: float) -> dict[str, Any]:
    return {"type": "noul", "noul": p}


def score(*probabilities: float) -> dict[str, Any]:
    return {"type": "score", "probabilities": {str(i): p for i, p in enumerate(probabilities)}}


EASY = score(1, 0, 0)


async def with_jev(conversation: Conversation, answers: Answers) -> FakeJev:
    jev = FakeJev(answers)
    conversation.settings = conversation.settings.model_copy(update={"decisions": Decisions()})
    conversation.overrides["api"] = jev
    await conversation.restart()
    return jev


async def test_a_passing_mention_of_the_name_is_ignored_and_an_address_answered(
    conversation: Conversation, model: ScriptedModel
) -> None:
    jev = await with_jev(
        conversation,
        lambda line: {"addressed": noul(0.95 if "17*23" in line else 0.05), "effort": EASY},
    )
    conversation.meet(FAY, PORCH)
    passing = await conversation.send(FAY, PORCH, "classic derp moment")
    await conversation.settle(passing)
    assert conversation.api.calls_of("sendRichMessage", after=passing.after) == []
    assert conversation.api.calls_of("sendChatAction", after=passing.after) == []  # not even "typing…"

    model.reply("391")
    asked = await conversation.send(FAY, PORCH, "дерп, сколько будет 17*23?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "391"
    await conversation.settle(asked)
    mentioned = await conversation.send(FAY, PORCH, "@DerpRobot hi", entities=[mention(0, 10)])
    model.reply("hey")
    await conversation.expect_reply(mentioned)
    await conversation.settle(mentioned)

    assert [questions for _, questions in jev.asked] == [["addressed", "effort"], ["addressed", "effort"]]
    assert jev.asked[1][0]["previous_messages"][-1].endswith("Fay: classic derp moment")
    async with conversation.db.session() as session:
        runs = list(await session.scalars(select(AgentRun.trigger_message_id).order_by(AgentRun.id)))
        ledgered = await session.scalar(select(func.count()).where(ProviderCall.kind == "decision"))
    assert runs == [asked.message_id, mentioned.message_id] and ledgered == 2


def mention(offset: int, length: int) -> dict[str, Any]:
    return {"type": "mention", "offset": offset, "length": length}


async def test_a_follow_up_photo_is_left_out_unless_the_question_needs_it(
    conversation: Conversation, model: ScriptedModel
) -> None:
    jev = await with_jev(
        conversation,
        lambda line: {
            "addressed": noul(0.9),
            "images": noul(0.97 if "breed" in line else 0.03),
            "effort": EASY,
        },
    )
    conversation.meet(GUS, PORCH)
    conversation.api.register_file("dog", PNG)
    posted = await conversation.send(GUS, PORCH, photo=photo("dog"))
    await conversation.settle(posted)

    model.reply(sees("sunny, 21°", count=0))  # no image, so no note either
    weather = await conversation.send(GUS, PORCH, "derp, weather in Paris tomorrow?")
    await conversation.expect_reply(weather)
    await conversation.settle(weather)
    assert len(model.requests) == 1

    again = await conversation.send(GUS, PORCH, photo=photo("dog"))
    await conversation.settle(again)
    model.reply(sees("a corgi"), "a corgi on grass")  # the answer, then the photo's note
    breed = await conversation.send(GUS, PORCH, "@DerpRobot what breed is this?", entities=[mention(0, 10)])
    await conversation.expect_reply(breed)
    await conversation.settle(breed)
    assert [questions for _, questions in jev.asked] == [
        ["addressed", "images", "effort"],
        ["images", "effort"],
    ]
    assert jev.asked[0][0]["images_from"][0].endswith(f"#{posted.message_id} Gus: [photo]")


async def test_a_hard_paid_question_reasons_deeper(conversation: Conversation, model: ScriptedModel) -> None:
    jev = await with_jev(
        conversation,
        lambda line: {"effort": score(0.01, 0.02, 0.97) if "prove" in line else EASY},
    )
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", FAY.id)).id, "stars:fay", 650, "purchase")
    model.reply("Suppose √2 = p/q in lowest terms…")
    hard = await conversation.send(FAY, text="prove that sqrt(2) is irrational")
    await conversation.expect_reply(hard)
    await conversation.settle(hard)
    model.reply("hi!")
    easy = await conversation.send(FAY, text="hi")
    await conversation.expect_reply(easy)
    await conversation.settle(easy)

    hard_settings, easy_settings = (info.model_settings or {} for info in model.infos)
    assert hard_settings.get("openrouter_reasoning") == {"effort": "high", "exclude": True}
    assert easy_settings.get("openrouter_reasoning") == {"effort": "low", "exclude": True}
    assert [questions for _, questions in jev.asked] == [
        ["effort"],
        ["effort"],
    ]  # one small call per paid turn
