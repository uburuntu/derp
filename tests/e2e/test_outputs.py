from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from derp.agent.models import RunOutput
from derp.history.models import Poll, Sticker
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

KATE = Person(1101, "Kate")
LEO = Person(1102, "Leo", username="leo")
PARTY = Room(-1013, title="Party")


def tool_results(messages: list[ModelMessage]) -> list[str]:
    return [p.model_response_str() for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


async def test_quiz_and_dice_go_out_as_recorded_outputs(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(KATE, PARTY)

    def both(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "send_poll",
                    {"question": "2+2?", "options": ["3", "4"], "correct_option": 1},
                    tool_call_id="p",
                ),
                ToolCallPart("send_dice", {"emoji": "🎯"}, tool_call_id="d"),
            ]
        )

    def wrap(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        results = tool_results(messages)
        assert any("Poll posted" in r for r in results) and any("landed on 4" in r for r in results), results
        return ModelResponse(parts=[TextPart("quiz time")])

    model.reply(both, wrap)
    asked = await conversation.send(KATE, PARTY, "дерп, устрой викторину")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "quiz time"
    (poll_call,) = conversation.api.calls_of("sendPoll", after=asked.after)
    assert (
        poll_call["type"] == "quiz" and int(poll_call["reply_parameters"]["message_id"]) == asked.message_id
    )
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        poll = await session.scalar(select(Poll))
        assert poll is not None and (poll.origin, poll.correct_option_ids) == ("derp_quiz", [1])
        kinds = sorted(await session.scalars(select(RunOutput.kind)))
        assert kinds == ["dice", "message", "poll"]


async def test_output_budget_keeps_a_slot_for_the_answer(
    conversation: Conversation, model: ScriptedModel
) -> None:
    def dice(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("send_dice", {}, tool_call_id=f"d{i}") for i in range(3)])

    def done(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert sum("output limit" in r for r in tool_results(messages)) == 1
        return ModelResponse(parts=[TextPart("two is plenty")])

    model.reply(dice, done)
    sent = await conversation.send(KATE, text="roll three dice")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "two is plenty"
    assert len(conversation.api.calls_of("sendDice", after=sent.after)) == 2


async def test_unconfirmed_outputs_are_never_called_refused_or_resent(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(KATE, PARTY)
    conversation.api.fail_next("sendDice", error_code=502, description="Bad Gateway")
    conversation.api.fail_next("setMessageReaction", error_code=500, description="Internal Server Error")

    def done(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        results = tool_results(messages)
        assert sum("didn't confirm" in r and "Don't send it again" in r for r in results) == 2, results
        assert not any("refused" in r for r in results), results
        return ModelResponse(parts=[TextPart("rolled, probably")])

    model.reply(tool_call("send_dice"), tool_call("react", emoji="🔥"), done)
    asked = await conversation.send(KATE, PARTY, "derp roll a dice")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "rolled, probably"
    await conversation.settle(asked)
    assert len(conversation.api.calls_of("sendDice", after=asked.after)) == 1  # never replayed
    async with conversation.db.session() as session:
        assert list(await session.scalars(select(RunOutput.kind))) == ["message"]


async def test_sticker_from_the_chat_replying_to_a_person(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(KATE, PARTY)
    conversation.meet(LEO, PARTY)
    sticker = {"file_id": "stk-1", "file_unique_id": "u-stk-1", "type": "regular", "width": 512, "height": 512,
               "is_animated": False, "is_video": False, "emoji": "😂", "set_name": "Funny"}  # fmt: skip
    posted = await conversation.send(LEO, PARTY, sticker=sticker)
    await conversation.settle(posted)

    model.reply(
        tool_call("send_sticker", emoji="😂", reply_to_person="@leo"),
        tool_call("send_sticker", emoji="🦖"),
        ".",
    )
    asked = await conversation.send(KATE, PARTY, "derp send leo a laughing sticker")
    await conversation.api.wait_call("sendSticker", after=asked.after)
    await conversation.settle(asked)
    (call,) = conversation.api.calls_of("sendSticker", after=asked.after)
    assert call["sticker"] == "stk-1" and int(call["reply_parameters"]["message_id"]) == posted.message_id
    assert (
        "No sticker for 🦖. Stickers I have: 😂;" in tool_results(model.requests[-1])[-1]
    )  # a miss lists what works
    texts = [
        str(c["rich_message"]["markdown"])
        for c in conversation.api.calls_of("sendRichMessage", after=asked.after)
    ]
    assert [
        x for x in texts if not x.startswith("👋")
    ] == []  # the sticker was the answer (only the join notice)


async def test_emoji_match_however_they_are_spelled(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(KATE, PARTY)
    conversation.meet(LEO, PARTY)
    sticker = {"file_id": "stk-love", "file_unique_id": "u-stk-love", "type": "regular", "width": 512,
               "height": 512, "is_animated": False, "is_video": False, "emoji": "❤️", "set_name": "Hearts"}  # fmt: skip
    posted = await conversation.send(LEO, PARTY, sticker=sticker)
    await conversation.settle(posted)
    async with conversation.db.session() as session:
        assert await session.scalar(select(Sticker.emoji)) == "❤"  # indexed plain, once

    model.reply(tool_call("send_sticker", emoji="❤"), tool_call("react", emoji="❤️"), ".")
    asked = await conversation.send(KATE, PARTY, "derp show leo some love")
    await conversation.settle(asked)
    (call,) = conversation.api.calls_of("sendSticker", after=asked.after)
    assert call["sticker"] == "stk-love"
    reactions = [c["reaction"] for c in conversation.api.calls_of("setMessageReaction", after=asked.after)]
    assert reactions == [[{"type": "emoji", "emoji": "❤"}]]  # Telegram's spelling, not a refusal


async def test_group_progress_is_a_chat_action_so_the_react_tool_sets_the_only_reaction(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(KATE, PARTY)
    # Progress in groups is a chat action, so a later tool call never puts a status emoji on the message.
    model.reply(tool_call("react", emoji="🔥"), tool_call("search_history", query="exam"), ".")
    asked = await conversation.send(KATE, PARTY, "derp I passed my exam!")
    await conversation.settle(asked)
    reactions = [c["reaction"] for c in conversation.api.calls_of("setMessageReaction", after=asked.after)]
    assert reactions == [[{"type": "emoji", "emoji": "🔥"}]]
    assert conversation.api.calls_of("sendChatAction", after=asked.after)
    async with conversation.db.session() as session:
        assert list(await session.scalars(select(RunOutput.kind))) == ["reaction"]


async def test_toolbox_tools_are_called_directly(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(KATE, PARTY)

    def use(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert "chat_info" in {t.name for t in info.function_tools}  # no search_tools round trip first
        return ModelResponse(parts=[ToolCallPart("chat_info", {}, tool_call_id="c")])

    model.reply(use, "it's the Party chat")
    asked = await conversation.send(KATE, PARTY, "derp what is this chat?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "it's the Party chat"
    assert any("Party" in r for r in tool_results(model.requests[-1]))
