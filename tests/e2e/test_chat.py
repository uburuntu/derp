from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from derp.agent.models import AgentRun, RunOutput
from derp.agent.persona import PERSONA
from derp.history.models import Message
from derp.inference.roles import DEFAULT_PINS, ModelRole
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

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
    async with conversation.db.session() as session:
        mine = await session.scalar(select(Message).where(Message.is_derp).order_by(Message.message_id))
        assert mine is not None and (mine.kind, mine.text) == ("text", "**привет**, человек")
    assert (
        model.pins[-1] == DEFAULT_PINS[ModelRole.CHAT_SPONSORED]
    )  # no credits bought: sponsored, no fast routing
    kinds = {tool.kind for tool in model.infos[-1].model_request_parameters.native_tools}
    assert kinds == {"web_fetch", "web_search"}  # a search's fee fits in the free daily hold

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
        assert info.instructions == PERSONA  # static, so tools + persona + window stay a cacheable prefix
        assert "<situation>" in prompt_text(messages[-1:]) and "Chaos" in prompt_text(messages[-1:])
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
    assert "nothing was charged" in reply["rich_message"]["markdown"]
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.error_type) == ("failed", "RuntimeError")


async def test_model_searches_history_with_a_tool(conversation: Conversation, model: ScriptedModel) -> None:
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    conversation.meet(BOB, Room(-1006, title="Food"))
    food = Room(-1006, title="Food")
    old = await conversation.send(BOB, food, "best shawarma is at Joe's")
    await conversation.settle(old)

    def call(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = {t.name for t in info.function_tools}
        assert names >= {"search_history", "read_messages", "chat_query", "chat_info", "make_meme"}
        assert "search_tools" not in names  # sponsored runs load every tool: a stable, cacheable tool list
        return ModelResponse(parts=[ToolCallPart("search_history", {"query": "shawarma"}, tool_call_id="c1")])

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        result = next(p for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        assert "Bob: best shawarma is at Joe's" in result.model_response_str()
        return ModelResponse(parts=[TextPart("Joe's, says Bob")])

    model.reply(call, answer)
    asked = await conversation.send(BOB, food, "derp where's the shawarma?")
    reply = await conversation.expect_reply(asked)
    assert reply["rich_message"]["markdown"] == "Joe's, says Bob"


async def test_consecutive_plain_derp_messages_keep_the_run_record_exact(
    conversation: Conversation, model: ScriptedModel
) -> None:
    from datetime import UTC, datetime

    from derp.agent.runs import load_messages
    from derp.history.models import Chat

    carl = Person(503, "Carl")
    conversation.meet(carl)
    async with conversation.db.transaction() as session:
        session.add(Chat(id=carl.id, type="private"))
        await session.flush()
        for _ in range(3):  # Derp messages with no replayable run (e.g. command replies)
            message_id = conversation.api.take_message_id(carl.id)
            session.add(
                Message(
                    chat_id=carl.id, message_id=message_id, date=datetime.now(UTC), kind="text",
                    text=f"note {message_id}", capture="derp", is_derp=True, payload={},
                )
            )  # fmt: skip
    model.reply("fresh answer")
    sent = await conversation.send(carl, text="hello?")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None
        stored = load_messages(run.messages)
    assert [type(m).__name__ for m in stored] == ["ModelRequest", "ModelResponse"]
    assert stored[-1].parts == [TextPart("fresh answer")]


async def test_redelivered_trigger_is_answered_once(conversation: Conversation, model: ScriptedModel) -> None:
    sent = await conversation.send(BOB, text="only once please")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    again = await conversation.push("message", sent.message)  # same message, new update id
    await conversation.settle(again)
    assert len(model.requests) == 1
    async with conversation.db.session() as session:
        runs = list(await session.scalars(select(AgentRun)))
    assert len(runs) == 1


async def test_a_reply_to_another_chat_shows_where_it_came_from(
    conversation: Conversation, model: ScriptedModel
) -> None:
    external = {
        "origin": {
            "type": "channel", "date": 1792000000, "message_id": 9,
            "chat": {"id": -100777, "type": "channel", "title": "Daily News"},
        },
        "chat": {"id": -100777, "type": "channel", "title": "Daily News"},
        "message_id": 9,
        "photo": [{"file_id": "far", "file_unique_id": "u-far", "width": 9, "height": 9}],
    }  # fmt: skip
    quote = {"text": "prices doubled", "position": 0}
    sent = await conversation.send(BOB, text="derp is this true?", external_reply=external, quote=quote)
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    current = prompt_text(model.requests[-1][-1:])
    assert "Bob ↩ (from Daily News: photo) «prices doubled»: derp is this true?" in current


async def test_group_answers_link_cited_messages_and_see_usernames(
    conversation: Conversation, model: ScriptedModel
) -> None:
    lounge = Room(-1001130715084, title="Lounge")
    rob, kim = Person(511, "Rob", username="rob_k"), Person(512, "Kim")
    conversation.meet(rob, lounge)
    conversation.meet(kim, lounge)
    chatter = await conversation.send(rob, lounge, "pizza tonight?")
    await conversation.settle(chatter)

    def check(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert "Rob @rob_k: pizza tonight?" in prompt_text(messages)  # the transcript names their @username
        return ModelResponse(parts=[TextPart(f"#{chatter.message_id} wants pizza, #999 is not a message")])

    model.reply(check)
    asked = await conversation.send(
        kim, lounge, "@DerpRobot who's hungry?", entities=[{"type": "mention", "offset": 0, "length": 10}]
    )
    reply = await conversation.expect_reply(asked)
    link = f"https://t.me/c/1130715084/{chatter.message_id}"
    assert (
        reply["rich_message"]["markdown"]
        == f"[#{chatter.message_id}]({link}) wants pizza, #999 is not a message"
    )
    await conversation.settle(asked)
    async with conversation.db.session() as session:  # history keeps the answer as written: no URLs
        answer = await session.scalar(select(Message).where(Message.is_derp))
    assert answer is not None and answer.text == f"#{chatter.message_id} wants pizza, #999 is not a message"

    # Edits link the same way; notes and jobs have their own numbers and never become links.
    fixed = f"#{chatter.message_id} wants pizza (note {chatter.message_id}, job {chatter.message_id})"
    model.reply(tool_call("edit_own_message", message_id=answer.message_id, new_text=fixed), "done")
    again = await conversation.send(
        kim, lounge, "@DerpRobot fix it", entities=[{"type": "mention", "offset": 0, "length": 10}]
    )
    await conversation.settle(again)
    (edit,) = conversation.api.calls_of("editMessageText", after=again.after)
    assert edit["rich_message"]["markdown"] == (
        f"[#{chatter.message_id}]({link}) wants pizza (note {chatter.message_id}, job {chatter.message_id})"
    )
    async with conversation.db.session() as session:
        edited = await session.get(Message, (lounge.id, answer.message_id))
    assert edited is not None and edited.text == fixed
