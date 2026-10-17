from typing import Any

from sqlalchemy import select, update

from derp.agent.models import AgentRun
from derp.history.models import Chat, Message, User
from derp.money.free import POLICY_VERSION
from derp.money.wallets import credit, wallet
from derp.scheduler.models import Job
from derp.settings.panel import SettingsAction
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

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
    assert "Context: **on**" in text and "Rules: Be kind" in text and "Only admins change" in text
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
    assert (
        "weren't addressed to me (**1**)"
        in conversation.api.ephemerals[(CLUBHOUSE.id, panel)]["rich_markdown"]
    )
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


def buttons(markup: dict[str, Any] | None) -> list[str]:
    return [b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row]


async def test_jobs_page_lets_people_manage_their_own_and_admins_all(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    conversation.meet(MEMBER, CLUBHOUSE)
    model.reply(tool_call("schedule", text="water the *plants* <b>now</b>", in_minutes=60), ".")
    await conversation.settle(await conversation.send(MEMBER, CLUBHOUSE, "derp remind me"))
    model.reply(tool_call("schedule", text="standup", in_minutes=30, repeat="daily"), ".")
    await conversation.settle(await conversation.send(ADMIN, CLUBHOUSE, "derp daily standup"))
    async with conversation.db.session() as session:
        ids = {job.text: job.id for job in await session.scalars(select(Job))}
    mine, theirs = ids["water the *plants* <b>now</b>"], ids["standup"]

    assert "Scheduled here: **2**" in await open_panel(conversation, MEMBER, 601)
    panel = max(k[1] for k in conversation.api.ephemerals if k[0] == CLUBHOUSE.id)
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("page", "jobs"))
    page = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]
    assert f"#{theirs}" in page["rich_markdown"] and "daily" in page["rich_markdown"]
    assert r"water the \*plants\* \<b\>now\<\/b\>" in page["rich_markdown"]  # shown literally
    assert set(buttons(page["reply_markup"])) == {
        data("jpause", str(mine)), data("jdrop", str(mine)), data("page", "main")
    }  # fmt: skip
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("jpause", str(mine)))
    async with conversation.db.session() as session:
        assert (await session.get(Job, mine)).state == "paused"  # type: ignore[union-attr]
    shown = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]
    assert data("jresume", str(mine)) in buttons(shown["reply_markup"])
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("jresume", str(mine)))
    async with conversation.db.session() as session:
        assert (await session.get(Job, mine)).state == "active"  # type: ignore[union-attr]

    await open_panel(conversation, ADMIN, 602)
    admin_panel = max(k[1] for k in conversation.api.ephemerals if k[0] == CLUBHOUSE.id)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("page", "jobs"))
    shown = conversation.api.ephemerals[(CLUBHOUSE.id, admin_panel)]
    assert data("jdrop", str(mine)) in buttons(shown["reply_markup"])
    conversation.api.set_member_status(chat_id=CLUBHOUSE.id, user_id=ADMIN.id, status="member")
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("jdrop", str(mine)))
    assert "Only its creator or an admin" in conversation.api.calls_of("answerCallbackQuery")[-1]["text"]
    shown = conversation.api.ephemerals[(CLUBHOUSE.id, admin_panel)]
    assert data("jdrop", str(mine)) not in buttons(shown["reply_markup"])  # redrawn for a member
    conversation.api.set_member_status(chat_id=CLUBHOUSE.id, user_id=ADMIN.id, status="administrator")
    for page_name in ("main", "jobs"):
        await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("page", page_name))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("jdrop", str(mine)))
    async with conversation.db.session() as session:
        assert (await session.get(Job, mine)).state == "cancelled"  # type: ignore[union-attr]
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("jdrop", str(theirs)))
    assert "Nothing scheduled" in conversation.api.ephemerals[(CLUBHOUSE.id, admin_panel)]["rich_markdown"]


async def test_a_job_of_another_chat_is_never_touched(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("schedule", text="mine", in_minutes=60), ".")
    await conversation.settle(await conversation.send(MEMBER, text="remind me"))
    async with conversation.db.session() as session:
        job_id = await session.scalar(select(Job.id))
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    async with conversation.db.transaction() as session:  # now a group's job, same creator
        await session.execute(update(Job).values(chat_id=CLUBHOUSE.id))
    asked = await conversation.send(MEMBER, text="/settings")
    await conversation.expect_reply(asked)
    panel_id = max(mid for (cid, mid) in conversation.api.messages if cid == MEMBER.id)
    forged = data("jdrop", str(job_id))
    conversation.api.messages[(MEMBER.id, panel_id)]["reply_markup"] = {
        "inline_keyboard": [[{"text": "x", "callback_data": forged}]]
    }  # a stale or forged button
    await conversation.press(MEMBER, MEMBER.id, panel_id, forged)
    assert "isn't scheduled" in conversation.api.calls_of("answerCallbackQuery")[-1]["text"]
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.state)) == "active"


async def test_retention_offers_configured_days_and_shortening_asks_first(conversation: Conversation) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    await open_panel(conversation, ADMIN, 701)
    panel = max(k[1] for k in conversation.api.ephemerals if k[0] == CLUBHOUSE.id)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "retention"))
    offered = buttons(conversation.api.ephemerals[(CLUBHOUSE.id, panel)]["reply_markup"])
    days = conversation.settings.retention.options
    assert [d for d in offered if d.startswith("set:retention:") and d != data("retention", "0")] == [
        data("retention", str(d)) for d in days
    ]
    shorter = min(days)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("retention", str(shorter)))
    assert f"older than {shorter} day" in conversation.api.ephemerals[(CLUBHOUSE.id, panel)]["rich_markdown"]
    async with conversation.db.session() as session:
        assert (await session.get(Chat, CLUBHOUSE.id)).retention_days is None  # type: ignore[union-attr]
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("shorten", str(shorter)))
    async with conversation.db.session() as session:
        assert (await session.get(Chat, CLUBHOUSE.id)).retention_days == shorter  # type: ignore[union-attr]
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "retention"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("retention", str(max(days))))
    async with conversation.db.session() as session:  # lengthening applies at once
        assert (await session.get(Chat, CLUBHOUSE.id)).retention_days == max(days)  # type: ignore[union-attr]
