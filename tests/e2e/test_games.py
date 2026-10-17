import json
from datetime import date
from typing import Any

from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from sqlalchemy import select

from derp.history.models import DailyTitle, GameScore
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


async def test_an_unconfirmed_poll_stops_the_series_without_a_resend(
    conversation: Conversation, model: ScriptedModel
) -> None:
    for person in (ADA, BEN):
        conversation.meet(person, QUIZ_ROOM)
    api = conversation.api

    def first_then_outage(fields: dict[str, Any]) -> Any:
        api.fail_next("sendPoll", error_code=502, description="Bad Gateway")  # the next poll's answer is lost
        return api._m_sendPoll(fields)  # the fake's own poll

    api.handle("sendPoll", first_then_outage)
    polls = [{"question": f"Q{i}?", "options": ["a", "b"], "correct": 0} for i in range(3)]
    model.reply(tool_call("post_polls", polls=polls), ".")
    asked = await conversation.send(ADA, QUIZ_ROOM, "derp quiz us")
    await conversation.settle(asked)
    # The second poll is not replayed and the third never goes.
    assert len(api.calls_of("sendPoll", after=asked.after)) == 2
    result = result_of(model, "post_polls")
    assert result.startswith("Posted 1 of 3 polls (#") and "didn't confirm the next poll" in result
    assert "refused" not in result


async def test_quoted_arguments_are_repaired(conversation: Conversation, model: ScriptedModel) -> None:
    """Some models (GLM) quote every argument: arrays as JSON text, numbers as strings, unset ones as ""."""
    for person in (ADA, BEN):
        conversation.meet(person, QUIZ_ROOM)
    polls = [{"question": "2+2?", "options": ["3", "4"], "correct": 1}]
    quoted_query = {
        "preset": "top_messages", "kinds": "[\"text\"]", "users": "[]", "days": "30", "since": "", "until": "",
        "media_type": "", "contains": "", "reaction": "", "min_reactions": "", "from_derp": "false",
        "group_by": "", "limit": 20, "whole_chat": True, "order": "count",
    }  # fmt: skip
    model.reply(
        tool_call("post_polls", polls=json.dumps(polls), seconds_per_poll="60"),
        tool_call("chat_query", **quoted_query),
        ".",
    )
    asked = await conversation.send(ADA, QUIZ_ROOM, "derp quiz us and show the top messages")
    await conversation.settle(asked)
    assert [p["type"] for p in conversation.api.calls_of("sendPoll", after=asked.after)] == ["quiz"]
    retries = [p for m in model.requests[-1] for p in m.parts if isinstance(p, RetryPromptPart)]
    assert retries == [] and result_of(model, "chat_query")


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


async def test_a_title_of_the_day_sticks_for_the_day_and_counts(
    conversation: Conversation, model: ScriptedModel
) -> None:
    for person in (ADA, BEN, CY):
        conversation.meet(person, QUIZ_ROOM)
    for person in (BEN, CY):
        await conversation.settle(await conversation.send(person, QUIZ_ROOM, "hi"))
    async with conversation.db.transaction() as session:
        session.add(DailyTitle(chat_id=QUIZ_ROOM.id, title="красавчик", day=date(2026, 1, 1), user_id=CY.id))
        session.add(DailyTitle(chat_id=QUIZ_ROOM.id, title="соня", day=date(2026, 1, 1), user_id=BEN.id))

    model.reply(tool_call("title_of_the_day", title="Красавчик"), "🎉")
    first = await conversation.send(ADA, QUIZ_ROOM, "дерп кто сегодня красавчик дня?")
    await conversation.settle(first)
    picked = result_of(model, "title_of_the_day")
    assert picked.startswith("красавчик of the day: ") and "just drawn" in picked
    winner = picked.removeprefix("красавчик of the day: ").split(",")[0]
    assert winner in {"Ada", "Ben", "Cy"}

    model.reply(tool_call("title_of_the_day", title="  красавчик "), "same as before")
    again = await conversation.send(BEN, QUIZ_ROOM, "derp reroll the красавчик")
    await conversation.settle(again)
    *_replayed, repeated = [
        str(p.content)
        for m in model.requests[-1]
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == "title_of_the_day"
    ]
    assert repeated.startswith(f"красавчик of the day: {winner}, already drawn earlier today")
    expected = "Cy 2" if winner == "Cy" else "Cy 1"
    assert expected in repeated and "соня" not in repeated

    model.reply(tool_call("chat_query", query={"preset": "title_leaderboard"}), "ok")
    board = await conversation.send(CY, QUIZ_ROOM, "derp title leaderboard?")
    await conversation.settle(board)
    lines = result_of(model, "chat_query").splitlines()
    assert "соня: Ben: 1" in lines and any(line.startswith("красавчик: ") for line in lines)


async def test_titles_of_the_day_need_a_group(conversation: Conversation, model: ScriptedModel) -> None:
    model.reply(tool_call("title_of_the_day", title="king"), "groups only")
    asked = await conversation.send(ADA, text="who's king of the day?")
    await conversation.settle(asked)
    assert result_of(model, "title_of_the_day") == "Titles of the day are a group game."
    async with conversation.db.session() as session:
        assert await session.scalar(select(DailyTitle.user_id)) is None
