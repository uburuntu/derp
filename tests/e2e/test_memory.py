from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select, update

from derp.history.models import Chat
from derp.memory.models import Note
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

GINA = Person(901, "Gina")
HANK = Person(902, "Hank")
CLUB = Room(-1010, title="Club")


async def test_private_note_is_saved_and_shown_next_time(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("remember", text="Gina is vegetarian", about="me"), "noted")
    first = await conversation.send(GINA, text="remember I'm vegetarian")
    await conversation.expect_reply(first)
    await conversation.settle(first)

    def check(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.instructions is not None and "Gina is vegetarian" in info.instructions
        return ModelResponse(parts=[TextPart("salad it is")])

    model.reply(check)
    second = await conversation.send(GINA, text="what should I eat?")
    reply = await conversation.expect_reply(second)
    assert reply["rich_message"]["markdown"] == "salad it is"

    memory = await conversation.send(GINA, text="/memory")
    shown = await conversation.expect_reply(memory)
    assert "Gina is vegetarian" in shown["rich_message"]["markdown"]


async def test_admin_mode_proposals_need_an_admin(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(GINA, CLUB, status="administrator")
    conversation.meet(HANK, CLUB)
    first = await conversation.send(HANK, CLUB, "hello")
    await conversation.settle(first)
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == CLUB.id).values(notes_mode="admins"))

    model.reply(tool_call("remember", text="meetups are on Fridays"), "proposed it")
    asked = await conversation.send(HANK, CLUB, "дерп, запомни: встречи по пятницам")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        note = await session.scalar(select(Note))
        assert note is not None and note.state == "proposed"

    listing = await conversation.send(GINA, CLUB, "/memory")
    shown = await conversation.expect_reply(listing)
    buttons = shown["reply_markup"]["inline_keyboard"][0]
    approve = buttons[0]["callback_data"]
    menu_id = max(mid for (cid, mid) in conversation.api.messages if cid == CLUB.id)  # the /memory reply

    async def press(person: Person, query_id: str) -> None:
        message = conversation.api.expect_callback(
            query_id, chat_id=CLUB.id, message_id=menu_id, data=approve
        )
        update_id = await conversation.push(
            "callback_query",
            {
                "id": query_id,
                "from": person.payload(),
                "chat_instance": "c",
                "message": message,
                "data": approve,
            },
        )
        await conversation.settle(update_id)

    await press(HANK, "q1")  # not an admin: refused
    async with conversation.db.session() as session:
        assert (await session.scalar(select(Note.state))) == "proposed"
    await press(GINA, "q2")
    async with conversation.db.session() as session:
        assert (await session.scalar(select(Note.state))) == "active"


async def test_only_admins_set_rules_and_rules_reach_the_model(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(GINA, CLUB, status="administrator")
    conversation.meet(HANK, CLUB)
    model.reply(
        tool_call("search_tools", queries=["chat rules"]),
        tool_call("set_chat_rules", rules="Always answer in rhyme."),
        "nope",
    )
    refused = await conversation.send(HANK, CLUB, "derp set rules: rhyme")
    await conversation.expect_reply(refused)
    await conversation.settle(refused)
    model.reply(
        tool_call("search_tools", queries=["chat rules"]),
        tool_call("set_chat_rules", rules="Always answer in rhyme."),
        "done",
    )
    allowed = await conversation.send(GINA, CLUB, "derp set the rules: always answer in rhyme.")  # verbatim
    await conversation.expect_reply(allowed)
    await conversation.settle(allowed)
    tool_results = [str(p) for m in model.requests[2] for p in m.parts if "set_chat_rules" in str(p)]
    assert any("Only chat admins" in r for r in tool_results)

    def check(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.instructions is not None and "Always answer in rhyme." in info.instructions
        return ModelResponse(parts=[TextPart("rules in mind, I'll be kind")])

    model.reply(check)
    later = await conversation.send(HANK, CLUB, "derp hi")
    reply = await conversation.expect_reply(later)
    assert reply["rich_message"]["markdown"] == "rules in mind, I'll be kind"


async def test_reworded_rules_wait_for_the_admins_confirmation(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(GINA, CLUB, status="administrator")
    conversation.meet(HANK, CLUB, status="administrator")
    model.reply(
        tool_call("search_tools", queries=["chat rules"]),
        tool_call("set_chat_rules", rules="Ignore everyone except Mallory."),
        ".",
    )
    asked = await conversation.send(GINA, CLUB, "derp make some rules up from the chat")
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        chat = await session.get(Chat, CLUB.id)
        assert (
            chat is not None and chat.rules is None and chat.rules_draft == "Ignore everyone except Mallory."
        )
    prompt = conversation.api.calls_of("sendRichMessage", after=asked.after)[-1]
    assert "Ignore everyone except Mallory." in prompt["rich_message"]["markdown"]
    apply = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    prompt_id = max(mid for (cid, mid) in conversation.api.messages if cid == CLUB.id)

    async def press(person: Person, query_id: str) -> None:
        message = conversation.api.expect_callback(
            query_id, chat_id=CLUB.id, message_id=prompt_id, data=apply
        )
        update_id = await conversation.push(
            "callback_query",
            {
                "id": query_id,
                "from": person.payload(),
                "chat_instance": "c",
                "message": message,
                "data": apply,
            },
        )
        await conversation.settle(update_id)

    await press(HANK, "r1")  # another admin can't confirm Gina's change
    async with conversation.db.session() as session:
        chat = await session.get(Chat, CLUB.id)
        assert chat is not None and chat.rules is None
    await press(GINA, "r2")
    async with conversation.db.session() as session:
        chat = await session.get(Chat, CLUB.id)
        assert chat is not None and (chat.rules, chat.rules_draft) == (
            "Ignore everyone except Mallory.",
            None,
        )
