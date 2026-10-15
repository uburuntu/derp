from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from derp.agent.models import AgentRun, RunOutput
from derp.history.models import Message
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

ANNA = Person(501, "Anna", language_code="ru")
BOB = Person(502, "Bob")
GROUP = Room(-1005, title="Chaos", is_forum=True)


def prompt_text(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    )


async def test_private_chat_answers_and_remembers(conversation: Conversation, model: ScriptedModel) -> None:
    model.reply("**привет**, человек", "ты спрашивала про котов")
    first = await conversation.send(ANNA, text="привет, дерп")
    reply = await conversation.expect_reply(first)
    assert reply["rich_message"]["markdown"] == "**привет**, человек"
    assert reply["reply_parameters"]["message_id"] == first.message_id
    await conversation.settle(first)

    second = await conversation.send(ANNA, text="о чём я спрашивала?")
    await conversation.expect_reply(second)
    await conversation.settle(second)
    history = model.requests[-1]
    assert "привет, дерп" in prompt_text(history)  # the previous turn is in the window
    assert any(isinstance(m, ModelResponse) and m.parts == [TextPart("**привет**, человек")] for m in history)
    assert "<current_message>" in prompt_text(history[-1:])
    assert model.pins[-1].model == "deepseek/deepseek-v4.1-flash"  # sponsored until wallets exist

    async with conversation.db.session() as session:
        runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.id)))
        assert [(r.status, r.funding, r.trigger_message_id) for r in runs] == [
            ("completed", "sponsored", first.message_id),
            ("completed", "sponsored", second.message_id),
        ]
        outputs = list(
            await session.scalars(select(RunOutput.message_id).where(RunOutput.run_id == runs[0].id))
        )
        derp_rows = list(await session.scalars(select(Message.message_id).where(Message.is_derp)))
        assert outputs and outputs[0] in derp_rows


async def test_group_mention_replies_in_topic_and_ambient_is_context(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ANNA, GROUP)
    conversation.meet(BOB, GROUP)
    chatter = await conversation.send(BOB, GROUP, "pizza tonight?", thread_id=9)
    elsewhere = await conversation.send(BOB, GROUP, "secret plans", thread_id=10)
    await conversation.settle(elsewhere)
    assert model.requests == []  # ambient messages never invoke

    def check(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = prompt_text(messages)
        assert "pizza tonight?" in text and "secret plans" not in text
        assert info.instructions is not None and "Chaos" in info.instructions
        return ModelResponse(parts=[TextPart("pineapple. fight me")])

    model.reply(check)
    asked = await conversation.send(
        ANNA,
        GROUP,
        "@DerpRobot what do you think?",
        thread_id=9,
        entities=[{"type": "mention", "offset": 0, "length": 10}],
    )
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "pineapple. fight me"  # the checks inside the model passed
    assert int(reply["message_thread_id"]) == 9
    assert reply["reply_parameters"]["message_id"] == asked.message_id
    assert chatter.message_id < asked.message_id


async def test_model_failure_apologizes_and_records_failed_run(
    conversation: Conversation, model: ScriptedModel
) -> None:
    def boom(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider exploded")

    model.reply(boom)
    sent = await conversation.send(BOB, text="hi")
    reply = await conversation.expect_reply(sent)
    assert "weren't charged" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.error_type) == ("failed", "RuntimeError")
