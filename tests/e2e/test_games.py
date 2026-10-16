from typing import Any

from pydantic_ai.messages import ToolReturnPart
from sqlalchemy import select

from derp.history.models import GameScore
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

ADA = Person(1801, "Ada")
BEN = Person(1802, "Ben")
CY = Person(1803, "Cy")
QUIZ_ROOM = Room(-1018, title="Quiz night")


def answer(poll_id: str, person: Person, *options: int) -> dict[str, Any]:
    return {
        "poll_id": poll_id,
        "user": person.payload(),
        "option_ids": list(options),
        "option_persistent_ids": [f"o{i}" for i in options],
    }


def result_of(model: ScriptedModel, tool: str) -> str:
    [found] = [
        str(p.content)
        for m in model.requests[-1]
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == tool
    ]
    return found


async def test_a_quiz_scores_first_answers_into_the_leaderboard(
    conversation: Conversation, model: ScriptedModel
) -> None:
    for person in (ADA, BEN, CY):
        conversation.meet(person, QUIZ_ROOM)
    polls = [
        {"question": "2+2?", "options": ["3", "4"], "correct": 1, "explanation": "Basic."},
        {"question": "Capital of France?", "options": ["Paris", "Rome", "Oslo"], "correct": 0},
        {"question": "Pizza or sushi?", "options": ["Pizza", "Sushi"], "multiple_answers": True},
    ]
    model.reply(
        tool_call("post_polls", polls=polls, seconds_per_poll=60),
        "Go!",
    )
    asked = await conversation.send(ADA, QUIZ_ROOM, "derp quiz us")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    sent = conversation.api.calls_of("sendPoll", after=asked.after)
    assert [p["type"] for p in sent] == ["quiz", "quiz", "regular"]
    assert all(str(p["is_anonymous"]).lower() == "false" and int(p["open_period"]) == 60 for p in sent)
    assert str(sent[2]["allows_multiple_answers"]).lower() == "true" and "explanation" not in sent[2].fields
    assert result_of(model, "post_polls").startswith("Posted 3 of 3 polls (#")

    for update in (
        answer("poll-1", BEN, 1),  # right
        answer("poll-1", BEN, 0),  # a redelivered or changed answer scores nothing
        answer("poll-2", BEN, 0),  # right
        answer("poll-1", CY, 0),  # wrong at zero stays zero
        answer("poll-2", CY, 0),  # right
        answer("poll-1", ADA, 0),  # wrong
        answer("poll-3", ADA, 0, 1),  # regular polls don't score
    ):
        await conversation.settle(await conversation.push("poll_answer", update))
    async with conversation.db.session() as session:
        scores = {(s.user_id, s.points, s.answers) for s in await session.scalars(select(GameScore))}
    assert scores == {(BEN.id, 2, 2), (CY.id, 1, 2), (ADA.id, 0, 1)}

    model.reply(tool_call("chat_query", query={"preset": "quiz_leaderboard", "days": 1}), "Ben wins.")
    board = await conversation.send(CY, QUIZ_ROOM, "derp who's winning?")
    await conversation.settle(board)
    assert result_of(model, "chat_query") == "Ben: 2\nCy: 1"


async def test_most_likely_polls_use_active_members(conversation: Conversation, model: ScriptedModel) -> None:
    for person in (ADA, BEN, CY):
        conversation.meet(person, QUIZ_ROOM)
    for person, text in ((BEN, "morning"), (CY, "hey"), (BEN, "coffee?")):
        await conversation.settle(await conversation.send(person, QUIZ_ROOM, text))
    model.reply(
        tool_call("most_likely_poll", question="Who's most likely to oversleep?"),
        ".",
    )
    asked = await conversation.send(ADA, QUIZ_ROOM, "derp make a most-likely-to poll about oversleeping")
    await conversation.settle(asked)
    [poll] = conversation.api.calls_of("sendPoll", after=asked.after)
    assert [o["text"] for o in poll["options"]] == ["Ben", "Ada", "Cy"] or {
        o["text"] for o in poll["options"]
    } == {"Ben", "Ada", "Cy"}
    assert str(poll["is_anonymous"]).lower() == "false"
