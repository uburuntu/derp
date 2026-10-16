from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from tests.e2e.harness import Conversation, Person
from tests.e2e.model import ScriptedModel

DORA = Person(901, "Dora")


async def test_questions_about_derp_are_answered_from_its_own_guide(
    conversation: Conversation, model: ScriptedModel
) -> None:
    plan = conversation.settings.plan

    def look_up(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = {tool.name for tool in info.function_tools}
        assert {"about_derp", "read_source", "search_source"} <= names
        return ModelResponse(parts=[ToolCallPart("about_derp", {"topics": ["money"]}, tool_call_id="a1")])

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        guide = next(p for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        assert f"Derp+: {plan.stars}⭐" in guide.model_response_str()
        return ModelResponse(parts=[TextPart(f"Derp+ is {plan.stars}⭐ a month")])

    model.reply(look_up, answer)
    asked = await conversation.send(DORA, text="how much is derp+?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == f"Derp+ is {plan.stars}⭐ a month"
