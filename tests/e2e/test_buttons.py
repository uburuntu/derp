from sqlalchemy import select

from derp.history.models import Chat, User
from derp.locales import t
from derp.memory.models import ReplyRule
from derp.settings.panel import SettingsAction
from derp.telegram.buttons import Dismiss, dismiss_button, markup
from tests.e2e.harness import Conversation, Person, Room

ADMIN = Person(2101, "Ada")
LOFT = Room(-2110, title="Loft")


async def test_dismiss_checks_the_actual_button_and_who_owns_it(conversation: Conversation) -> None:
    member = Person(2102, "Jo")
    conversation.meet(ADMIN, LOFT, status="administrator")
    conversation.meet(member, LOFT)
    assert conversation.services is not None
    for owner, denied, allowed in [(None, member, ADMIN), (member.id, ADMIN, member)]:
        ok = dismiss_button("ru", user_id=owner)
        message = await conversation.services.bot.send_message(LOFT.id, "Сохранено", reply_markup=markup(ok))
        assert ok.text == "ОК" and ok.callback_data
        await conversation.press(denied, LOFT.id, message.message_id, ok.callback_data)
        assert conversation.api.calls_of("answerCallbackQuery")[-1]["show_alert"]
        assert "reply_markup" in conversation.api.message(LOFT.id, message.message_id)
        # A client can send callback data missing from the card. Even an admin can't use it to
        # dismiss some other workflow or replace a user-bound OK with their own.
        cid = f"forged-{message.message_id}"
        conversation.api.outstanding_callbacks.add(cid)
        await conversation.settle(
            await conversation.push(
                "callback_query",
                {
                    "id": cid,
                    "from": ADMIN.payload(),
                    "chat_instance": "group",
                    "message": conversation.api.message(LOFT.id, message.message_id),
                    "data": Dismiss(user_id=ADMIN.id).pack(),
                },
            )
        )
        assert conversation.api.calls_of("answerCallbackQuery")[-1]["show_alert"]
        assert "reply_markup" in conversation.api.message(LOFT.id, message.message_id)
        await conversation.press(allowed, LOFT.id, message.message_id, ok.callback_data)
        final = conversation.api.message(LOFT.id, message.message_id)
        assert final["text"] == "Сохранено" and "reply_markup" not in final


def data(action: str, value: str = "") -> str:
    return SettingsAction(action=action, value=value).pack()


async def open_panel(conversation: Conversation, person: Person, ephemeral_id: int) -> int:
    await conversation.settle(await conversation.ephemeral_command(person, LOFT, "/settings", ephemeral_id))
    return max(k[1] for k in conversation.api.ephemerals if k[0] == LOFT.id)


async def test_a_double_tap_is_answered_twice_and_applied_once(conversation: Conversation) -> None:
    conversation.meet(ADMIN, LOFT, status="administrator")
    await conversation.settle(await conversation.send(ADMIN, LOFT, "hi"))
    panel = await open_panel(conversation, ADMIN, 901)
    await conversation.press_ephemeral(ADMIN, LOFT.id, panel, data("page", "funding"))
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
    assert "mentions and replies" in panel["rich_message"]["markdown"].lower()
    panel_id = max(mid for cid, mid in conversation.api.messages if cid == LOFT.id)
    public = conversation.api.message(LOFT.id, panel_id)

    def controls() -> list[dict[str, str]]:
        return [
            b
            for row in conversation.api.message(LOFT.id, panel_id)["reply_markup"]["inline_keyboard"]
            for b in row
        ]

    assert not any(b.get("callback_data") == data("page", "personal") for b in controls())
    # An old client may send a personal callback on a public card. Never change anybody's choice
    # or turn that shared message into a personal panel.
    for action, value in [("page", "personal"), ("spend", "off"), ("named", "off")]:
        cid = f"private-on-public-{action}"
        conversation.api.outstanding_callbacks.add(cid)
        await conversation.settle(
            await conversation.push(
                "callback_query",
                {
                    "id": cid,
                    "from": ADMIN.payload(),
                    "chat_instance": "group",
                    "message": public,
                    "data": data(action, value),
                },
            )
        )
        assert conversation.api.calls_of("answerCallbackQuery")[-1]["show_alert"]
    assert not conversation.api.calls_of("editMessageText")
    async with conversation.db.session() as session:
        user = await session.get(User, ADMIN.id)
        assert user and user.patron_named and user.group_spending

    await conversation.press(ADMIN, LOFT.id, panel_id, data("page", "history"))
    history = conversation.api.calls_of("editMessageText")[-1]["rich_message"]["markdown"]
    assert "messages addressed to me" in history
    assert any(
        b.get("url") == "https://t.me/DerpRobot?startgroup=derp&admin=invite_users+pin_messages"
        for b in controls()
    )
    member = Person(2102, "Jo")
    conversation.meet(member, LOFT)
    # This public admin button is real, but the actual viewer changed.
    await conversation.press(member, LOFT.id, panel_id, data("page", "retention"))
    assert conversation.api.calls_of("answerCallbackQuery")[-1]["show_alert"]
    await conversation.press(member, LOFT.id, panel_id, data("page", "main"))
    await conversation.press(member, LOFT.id, panel_id, data("page", "history"))
    assert not any(b.get("url") or b.get("callback_data") == data("page", "retention") for b in controls())

    async with conversation.db.transaction() as session:
        session.add(
            ReplyRule(
                chat_id=LOFT.id,
                admin_id=ADMIN.id,
                condition="a dog photo",
                action="ask about the dog",
                state="active",
            )
        )
    await conversation.press(member, LOFT.id, panel_id, data("page", "main"))
    overview = conversation.api.calls_of("editMessageText")[-1]["rich_message"]["markdown"]
    assert t("en", "overview.rules_limited", count=1) in overview
    await conversation.press(member, LOFT.id, panel_id, data("page", "behavior"))
    behavior = conversation.api.message(LOFT.id, panel_id)
    assert t("en", "sections.triggers_context_needs_admin") in "\n".join(
        b["text"] for b in behavior["rich_message"]["blocks"]
    )
