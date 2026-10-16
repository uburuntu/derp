from sqlalchemy import select, update

from derp.agent.models import AgentRun
from derp.history.models import Chat, Message, User
from derp.money.free import POLICY_VERSION
from derp.money.wallets import credit, wallet
from derp.settings.panel import SettingsAction
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel

ADMIN = Person(1901, "Ida")
MEMBER = Person(1902, "Moe")
CLUBHOUSE = Room(-1020, title="Clubhouse")


def data(action: str, value: str = "") -> str:
    return SettingsAction(action=action, value=value).pack()


async def open_panel(conversation: Conversation, person: Person, ephemeral_id: int) -> str:
    await conversation.settle(
        await conversation.ephemeral_command(person, CLUBHOUSE, "/settings", ephemeral_id)
    )
    [panel] = [
        c
        for c in conversation.api.calls_of("sendRichMessage")
        if c.fields.get("ephemeral_message_parameters")
    ][-1:]
    assert int(panel["ephemeral_message_parameters"]["receiver_user_id"]) == person.id
    return str(panel["rich_message"]["markdown"])


async def test_admins_change_chat_settings_and_members_only_their_own(conversation: Conversation) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    conversation.meet(MEMBER, CLUBHOUSE)
    chatter = await conversation.send(MEMBER, CLUBHOUSE, "anyone up for pizza")
    await conversation.settle(chatter)
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == CLUBHOUSE.id).values(rules="Be kind."))

    text = await open_panel(conversation, MEMBER, 501)
    assert "Context: **on**" in text and "Rules: Be kind." in text and "Only admins change" in text
    member_panel = max(k[1] for k in conversation.api.ephemerals if k[0] == CLUBHOUSE.id)
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, member_panel, data("page", "retention"))
    assert (
        conversation.api.calls_of("answerCallbackQuery")[-1]["text"]
        == "Only admins can change this chat's settings."
    )
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, member_panel, data("spend", "off"))
    async with conversation.db.session() as session:
        assert (await session.get(User, MEMBER.id)).group_spending is False  # type: ignore[union-attr]

    await open_panel(conversation, ADMIN, 502)
    panel = max(k[1] for k in conversation.api.ephemerals if k[0] == CLUBHOUSE.id)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "retention"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("retention", "30"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "notes"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("notes", "admins"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("rules", "clear"))
    after = conversation.seq
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("free", "on"))
    [notice] = conversation.api.calls_of("sendRichMessage", after=after)
    assert not notice.fields.get(
        "ephemeral_message_parameters"
    )  # turning free models on is announced publicly
    shown = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]["rich_markdown"]
    assert "**30 days**" in shown and "admins only" in shown and "Rules: none" in shown

    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "ambient_off"))
    assert "**1** stored messages" in conversation.api.ephemerals[(CLUBHOUSE.id, panel)]["rich_markdown"]
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("ambient", "purge"))
    async with conversation.db.session() as session:
        chat = await session.get(Chat, CLUBHOUSE.id)
        assert chat is not None and (
            chat.ambient,
            chat.retention_days,
            chat.notes_mode,
            chat.free_models,
            chat.rules,
        ) == (False, 30, "admins", True, None)
        assert (
            await session.scalar(select(Message.message_id).where(Message.message_id == chatter.message_id))
            is None
        )


async def test_opted_out_members_coins_are_left_alone_in_groups(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(MEMBER, CLUBHOUSE)
    async with conversation.db.transaction() as session:
        await credit(session, (await wallet(session, "user", MEMBER.id)).id, "stars:moe", 650, "purchase")
    asked = await conversation.send(MEMBER, CLUBHOUSE, "derp hi")
    await conversation.settle(asked)
    async with conversation.db.transaction() as session:
        await session.execute(update(User).where(User.id == MEMBER.id).values(group_spending=False))
    again = await conversation.send(MEMBER, CLUBHOUSE, "derp hi again")
    await conversation.settle(again)
    async with conversation.db.session() as session:
        fundings = list(await session.scalars(select(AgentRun.funding).order_by(AgentRun.id)))
    assert fundings == ["paid", "sponsored"]
    private = await conversation.send(MEMBER, text="hi")  # their own chat always uses their coins
    await conversation.settle(private)
    async with conversation.db.session() as session:
        assert await session.scalar(select(AgentRun.funding).order_by(AgentRun.id.desc())) == "paid"


async def test_private_panel_toggles_free_model_consent(conversation: Conversation) -> None:
    asked = await conversation.send(MEMBER, text="/settings")
    reply = await conversation.expect_reply(asked)
    assert "Context:" not in reply["rich_message"]["markdown"]
    panel_id = max(mid for (cid, mid) in conversation.api.messages if cid == MEMBER.id)
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("free", "on"))
    async with conversation.db.session() as session:
        user = await session.get(User, MEMBER.id)
        assert user is not None and user.free_models_consent_version == POLICY_VERSION
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("free", "off"))
    async with conversation.db.session() as session:
        user = await session.get(User, MEMBER.id)
        assert user is not None and user.free_models_consent_version is None
