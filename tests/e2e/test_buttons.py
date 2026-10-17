from sqlalchemy import select

from derp.history.models import Chat
from derp.settings.panel import SettingsAction
from tests.e2e.harness import Conversation, Person, Room

ADMIN = Person(2101, "Ada")
LOFT = Room(-2110, title="Loft")


def data(action: str, value: str = "") -> str:
    return SettingsAction(action=action, value=value).pack()


async def open_panel(conversation: Conversation, person: Person, ephemeral_id: int) -> int:
    await conversation.settle(await conversation.ephemeral_command(person, LOFT, "/settings", ephemeral_id))
    return max(k[1] for k in conversation.api.ephemerals if k[0] == LOFT.id)


async def test_a_double_tap_is_answered_twice_and_applied_once(conversation: Conversation) -> None:
    conversation.meet(ADMIN, LOFT, status="administrator")
    await conversation.settle(await conversation.send(ADMIN, LOFT, "hi"))
    panel = await open_panel(conversation, ADMIN, 901)
    after = conversation.seq
    await conversation.press_ephemeral(ADMIN, LOFT.id, panel, data("free", "on"), taps=2)
    assert len(conversation.api.calls_of("answerCallbackQuery", after=after)) == 2
    public = [
        c
        for c in conversation.api.calls_of("sendRichMessage", after=after)
        if not c.fields.get("ephemeral_message_parameters")
    ]
    assert len(public) == 1  # announced once, not once per tap
    async with conversation.db.session() as session:
        assert await session.scalar(select(Chat.free_models).where(Chat.id == LOFT.id))


async def test_a_press_on_a_message_derp_cant_see_gets_the_stale_alert(conversation: Conversation) -> None:
    conversation.meet(ADMIN, LOFT, status="administrator")
    after = conversation.seq
    await conversation.press_stale(ADMIN, LOFT.id, 4242, data("page", "main"))
    [answer] = conversation.api.calls_of("answerCallbackQuery", after=after)
    assert answer["text"] == "These buttons are out of date." and answer["show_alert"]
    assert {c.method for c in conversation.api.calls[after:]} == {"getUpdates", "answerCallbackQuery"}


async def test_a_button_from_an_older_version_gets_the_stale_alert(conversation: Conversation) -> None:
    conversation.meet(ADMIN, LOFT)
    sent = await conversation.send(ADMIN, LOFT, "hi")
    await conversation.settle(sent)
    old = {"text": "⭐ Buy", "callback_data": "sa:buy"}  # no handler knows it any more
    conversation.api.messages[(LOFT.id, sent.message_id)]["reply_markup"] = {"inline_keyboard": [[old]]}
    after = conversation.seq
    await conversation.press(ADMIN, LOFT.id, sent.message_id, "sa:buy")
    [answer] = conversation.api.calls_of("answerCallbackQuery", after=after)
    assert answer["text"] == "These buttons are out of date." and answer["show_alert"]


async def test_settings_offer_the_admin_link_when_derp_cant_read_the_chat(conversation: Conversation) -> None:
    conversation.api.bot_user["can_read_all_group_messages"] = False  # privacy mode on, Derp not an admin
    await conversation.restart()
    conversation.meet(ADMIN, LOFT, status="administrator")
    await conversation.settle(await conversation.send(ADMIN, LOFT, "/settings@DerpRobot"))
    panel = conversation.api.calls_of("sendRichMessage")[-1]
    assert "make me an admin" in panel["rich_message"]["markdown"]
    first = panel["reply_markup"]["inline_keyboard"][0][0]
    assert first["url"] == "https://t.me/DerpRobot?startgroup=derp&admin=invite_users"
    assert first["text"] == "👀 Let me read the chat (make me admin)"
