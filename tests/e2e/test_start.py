from sqlalchemy import func, select

from derp.history.models import Message
from derp.settings.start import StartAction, publish_commands
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

ANNA = Person(id=101, first_name="Anna", language_code="ru")
BEN = Person(id=102, first_name="Ben")
LOUNGE = Room(-1030, title="Lounge")


async def test_private_start_replies_in_users_language(conversation: Conversation) -> None:
    sent = await conversation.send(ANNA, text="/start")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"].startswith("**Дерп**")
    assert reply["reply_parameters"]["message_id"] == sent.message_id


async def test_start_showcases_and_leads_to_buying(conversation: Conversation) -> None:
    sent = await conversation.send(BEN, text="/start")
    reply = await conversation.expect_reply(sent)
    text = reply["rich_message"]["markdown"]
    assert "<details><summary>🎨 Pictures and memes</summary>" in text and "@DerpRobot" in text
    add, buy = reply["reply_markup"]["inline_keyboard"][0]
    assert add["url"] == "https://t.me/DerpRobot?startgroup=derp"
    assert buy["callback_data"] == StartAction(action="buy").pack()

    showcase = max(mid for (cid, mid) in conversation.api.messages if cid == BEN.id)
    after = conversation.seq
    await conversation.press(BEN, BEN.id, showcase, buy["callback_data"])
    [menu] = conversation.api.calls_of("sendRichMessage", after)
    assert menu["rich_message"]["markdown"].startswith("**Derpcoins**")

    deep_link = await conversation.send(BEN, text="/start buy")
    menu = await conversation.expect_reply(deep_link)
    assert "Your balance: **0**" in menu["rich_message"]["markdown"]


async def test_help_describes_the_chat(conversation: Conversation) -> None:
    sent = await conversation.send(BEN, text="/help")
    text = (await conversation.expect_reply(sent))["rich_message"]["markdown"]
    free = conversation.settings.sponsored_daily_credits
    assert f"Derpcoins: **{free}** of {free} free left today, **0** bought" in text and "/memory" in text
    assert (
        "**Commands**  \n/settings — chat and personal settings  \n" in text
    )  # each command on its own line

    conversation.meet(BEN, LOUNGE)
    await conversation.settle(await conversation.ephemeral_command(BEN, LOUNGE, "/help", 601))
    reply = conversation.api.calls_of("sendRichMessage")[-1]
    assert int(reply["ephemeral_message_parameters"]["receiver_user_id"]) == BEN.id
    text = reply["rich_message"]["markdown"]
    assert text.startswith("**Derp in Lounge**") and "@DerpRobot" in text
    assert "I read this chat" in text and "This chat's wallet: **0** dc" in text


async def test_unknown_commands_point_to_help_without_a_model(
    conversation: Conversation, model: ScriptedModel
) -> None:
    sent = await conversation.send(BEN, text="/joke please")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"].startswith("There's no /joke.")

    conversation.meet(BEN, LOUNGE)
    bare = await conversation.send(BEN, LOUNGE, "/roll")  # may be meant for another bot
    await conversation.settle(bare)
    assert not conversation.api.calls_of("sendRichMessage", bare.after)
    addressed = await conversation.send(BEN, LOUNGE, "/roll@DerpRobot")
    assert "/roll" in (await conversation.expect_reply(addressed))["rich_message"]["markdown"]

    assert not model.requests
    async with conversation.db.session() as session:  # commands are controls, never history
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


async def test_command_menus_per_scope_and_language(conversation: Conversation) -> None:
    assert conversation.services is not None
    await publish_commands(conversation.services.bot)
    calls = conversation.api.calls_of("setMyCommands")
    assert len(calls) == 4
    groups_ru = next(
        c for c in calls if c["scope"]["type"] == "all_group_chats" and c.fields.get("language_code") == "ru"
    )
    commands = {c["command"]: c for c in groups_ru["commands"]}
    assert commands["credits"]["is_ephemeral"] is True and "is_ephemeral" not in commands["buy"]
    assert commands["buy"]["description"] == "Купить дерпкоины" and "start" not in commands
    private = next(
        c for c in calls if c["scope"]["type"] == "all_private_chats" and not c.fields.get("language_code")
    )
    assert [c["command"] for c in private["commands"]][:2] == ["start", "help"]


async def test_bare_group_commands_and_edited_commands_are_left_alone(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(BEN, LOUNGE)
    bare = await conversation.send(BEN, LOUNGE, "/help")  # may be meant for another bot
    await conversation.settle(bare)
    assert not conversation.api.calls_of("sendRichMessage", bare.after)

    model.reply("hi Ben")
    chat = await conversation.send(BEN, text="hello")  # a sponsored answer opens today's free lot
    await conversation.expect_reply(chat)
    await conversation.settle(chat)
    helped = await conversation.send(BEN, text="/help")
    assert ", **0** bought" in (await conversation.expect_reply(helped))["rich_message"]["markdown"]

    edited = {**helped.message, "text": "/help me", "edit_date": int(helped.message["date"]) + 5}
    await conversation.settle(await conversation.push("edited_message", edited))
    async with conversation.db.session() as session:
        stored = list(await session.scalars(select(Message.text).where(Message.chat_id == BEN.id)))
    assert "/help me" not in stored and "/help" not in stored
