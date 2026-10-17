import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select, update

from derp.history.models import Chat
from derp.memory.models import Note
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_effects import results_of

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

    def check(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert "Gina is vegetarian" in str(messages[-1])  # the situation opens the current request
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
        tool_call("set_chat_rules", rules="Always answer in rhyme."),
        "nope",
    )
    refused = await conversation.send(HANK, CLUB, "derp set rules: rhyme")
    await conversation.expect_reply(refused)
    await conversation.settle(refused)
    model.reply(
        tool_call("set_chat_rules", rules="Always answer in rhyme."),
        "done",
    )
    allowed = await conversation.send(GINA, CLUB, "derp set the rules: always answer in rhyme.")  # verbatim
    await conversation.expect_reply(allowed)
    await conversation.settle(allowed)
    tool_results = [str(p) for m in model.requests[2] for p in m.parts if "set_chat_rules" in str(p)]
    assert any("Only chat admins" in r for r in tool_results)

    def check(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        assert "Always answer in rhyme." in str(messages[-1])
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


async def test_a_public_memory_reply_keeps_personal_notes_out(conversation: Conversation) -> None:
    conversation.meet(GINA, CLUB)
    async with conversation.db.transaction() as session:
        session.add(Note(chat_id=CLUB.id, user_id=GINA.id, text="Gina hates mondays", author_id=GINA.id))
    public = await conversation.send(GINA, CLUB, "/memory@DerpRobot")
    text = (await conversation.expect_reply(public))["rich_message"]["markdown"]
    assert "hates mondays" not in text and "Your own notes stay private" in text

    await conversation.settle(await conversation.ephemeral_command(GINA, CLUB, "/memory", 811))
    assert "hates mondays" in conversation.api.calls_of("sendRichMessage")[-1]["rich_message"]["markdown"]


async def test_a_decided_proposal_loses_its_buttons_and_the_rest_keep_theirs(
    conversation: Conversation,
) -> None:
    conversation.meet(GINA, CLUB, status="administrator")
    await conversation.settle(await conversation.send(GINA, CLUB, "hello"))
    async with conversation.db.transaction() as session:
        first = Note(chat_id=CLUB.id, text="meetups are on Fridays", author_id=HANK.id, state="proposed")
        second = Note(chat_id=CLUB.id, text="the cat is called Bob", author_id=HANK.id, state="proposed")
        session.add_all([first, second])
        await session.flush()
        ids = first.id, second.id
    listing = await conversation.send(GINA, CLUB, "/memory")
    shown = await conversation.expect_reply(listing)
    rows = shown["reply_markup"]["inline_keyboard"]
    assert sorted(row[0]["text"] for row in rows) == [f"✅ #{i}" for i in sorted(ids)]
    approved = int(rows[0][0]["text"].split("#")[1])
    menu_id = max(mid for (cid, mid) in conversation.api.messages if cid == CLUB.id)

    after = conversation.seq
    await conversation.press(GINA, CLUB.id, menu_id, rows[0][0]["callback_data"])
    toast = conversation.api.calls_of("answerCallbackQuery", after=after)[-1]
    assert toast["text"] == f"Note #{approved} saved." and not toast.fields.get("show_alert")
    left = conversation.api.message(CLUB.id, menu_id)["reply_markup"]["inline_keyboard"]
    assert left == [rows[1]]  # only the undecided proposal keeps its buttons

    after = conversation.seq
    await conversation.press(GINA, CLUB.id, menu_id, rows[1][1]["callback_data"], taps=2)
    answers = conversation.api.calls_of("answerCallbackQuery", after=after)
    assert len(answers) == 2 and answers[1]["text"] == "Already handled." and answers[1]["show_alert"]
    assert "reply_markup" not in conversation.api.message(CLUB.id, menu_id)
    async with conversation.db.session() as session:
        assert [n.state for n in await session.scalars(select(Note))] == ["active"]


@pytest.mark.parametrize(
    ("mode", "asker", "about", "state", "says"),
    [
        ("auto", HANK, "chat", "active", "on your own"),
        ("members", HANK, "chat", "active", "only when someone explicitly asks"),
        ("admins", HANK, "chat", "proposed", "goes to the admins"),
        ("admins", GINA, "chat", "active", "goes to the admins"),
        ("off", HANK, "chat", None, "don't save any"),
        ("off", HANK, "me", "active", "don't save any"),  # personal notes work in every mode
    ],
)
async def test_remember_follows_the_chat_notes_mode(
    conversation: Conversation,
    model: ScriptedModel,
    mode: str,
    asker: Person,
    about: str,
    state: str | None,
    says: str,
) -> None:
    conversation.meet(GINA, CLUB, status="administrator")
    conversation.meet(HANK, CLUB)
    first = await conversation.send(HANK, CLUB, "hello")
    await conversation.settle(first)
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == CLUB.id).values(notes_mode=mode))

    model.reply(tool_call("remember", text="meetups are on Fridays", about=about), "ok")
    asked = await conversation.send(asker, CLUB, "derp, remember: meetups are on Fridays")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert says in str(model.requests[0][-1])  # the policy line is in the situation
    async with conversation.db.session() as session:
        note = await session.scalar(select(Note))
    assert (note.state if note else None) == state
    if state is None:
        assert "turned off" in results_of(model, "remember")[0]
