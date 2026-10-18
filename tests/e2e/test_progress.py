from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from derp.agent.models import AgentRun
from derp.agent.runtime import Runtime
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import Chunks, ScriptedModel

FRED = Person(801, "Fred")
GROUP = Room(-1009, title="Progress")


MENTION = [{"type": "mention", "offset": 0, "length": 10}]  # "@DerpRobot"


async def test_private_answer_streams_into_a_stoppable_draft(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(Chunks(["Once upon", " a time", " there was a cat."]))
    sent = await conversation.send(FRED, text="tell me a story")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "Once upon a time there was a cat."
    drafts = conversation.api.calls_of("sendRichMessageDraft", after=sent.after)
    assert drafts, "the answer streamed into a draft first"
    assert all(str(d["can_stop"]).lower() == "true" for d in drafts)
    assert "<tg-thinking>" in drafts[0]["rich_message"]["markdown"]
    assert len({d["draft_id"] for d in drafts}) == 1
    assert drafts[-1].sequence < reply.sequence


async def test_stop_button_posts_the_partial_answer(conversation: Conversation, model: ScriptedModel) -> None:
    assert conversation.dispatcher is not None
    runtime: Runtime = conversation.dispatcher["runtime"]

    async def press_stop(index: int) -> None:
        if index == 0:
            draft = await conversation.api.wait_call("sendRichMessageDraft")
            assert runtime.stop(FRED.id, int(draft["draft_id"]))

    model.reply(Chunks(["The answer is", " forty", "-two"], between=press_stop))
    sent = await conversation.send(FRED, text="meaning of life?")
    reply = await conversation.expect_reply(sent)
    partial = reply["rich_message"]["markdown"]
    assert partial.startswith("The answer is") and not partial.endswith("-two")  # stopped mid-answer
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.status == "cancelled"
    assert not runtime.active


async def test_group_progress_is_a_chat_action_never_a_reaction(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(FRED, GROUP)

    def search(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("search_history", {"queries": ["x"]}, tool_call_id="s1")])

    model.reply(search, "nothing found")
    asked = await conversation.send(FRED, GROUP, "@DerpRobot find x", entities=MENTION)
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    actions = conversation.api.calls_of("sendChatAction", after=asked.after)
    assert actions and {a["action"] for a in actions} == {"typing"}
    assert conversation.api.calls_of("setMessageReaction", after=asked.after) == []
    assert conversation.api.calls_of("sendRichMessageDraft", after=asked.after) == []
