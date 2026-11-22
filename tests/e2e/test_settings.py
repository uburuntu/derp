from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from derp.agent.models import AgentRun
from derp.history.models import Chat, Message, User
from derp.inference.models import ProviderCall
from derp.memory.models import ReplyRule
from derp.money.free import POLICY_VERSION
from derp.money.models import CreditLot, LedgerEntry, Reservation, Wallet
from derp.money.wallets import credit, wallet
from derp.scheduler.models import Job
from derp.settings.panel import SettingsAction
from derp.telegram.buttons import Dismiss, button, markup
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

ADMIN = Person(1901, "Ida")
MEMBER = Person(1902, "Moe")
CLUBHOUSE = Room(-1020, title="Clubhouse")


def data(action: str, value: str = "") -> str:
    return SettingsAction(action=action, value=value).pack()


async def open_panel(conversation: Conversation, person: Person, ephemeral_id: int) -> int:
    await conversation.settle(
        await conversation.ephemeral_command(person, CLUBHOUSE, "/settings", ephemeral_id)
    )
    panel = conversation.api.calls_of("sendRichMessage")[-1]
    assert int(panel["ephemeral_message_parameters"]["receiver_user_id"]) == person.id
    return max(mid for cid, mid in conversation.api.ephemerals if cid == CLUBHOUSE.id)


def buttons(markup: dict[str, Any] | None) -> list[str]:
    return [
        b["callback_data"]
        for row in (markup or {}).get("inline_keyboard", [])
        for b in row
        if "callback_data" in b
    ]


def visible(message: dict[str, Any]) -> str:
    if message.get("rich_markdown"):
        return str(message["rich_markdown"])
    return "\n".join(block["text"] for block in message["rich_message"]["blocks"])


async def test_admins_change_chat_settings_and_members_only_their_own(conversation: Conversation) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    conversation.meet(MEMBER, CLUBHOUSE)
    chatter = await conversation.send(MEMBER, CLUBHOUSE, "anyone up for pizza")
    await conversation.settle(chatter)
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == CLUBHOUSE.id).values(rules="Be kind."))

    panel = await open_panel(conversation, MEMBER, 501)

    def page() -> dict[str, Any]:
        return conversation.api.ephemerals[(CLUBHOUSE.id, panel)]

    async def tap(person: Person, action: str, value: str) -> None:
        await conversation.press_ephemeral(person, CLUBHOUSE.id, panel, data(action, value))

    assert {k for k in buttons(page()["reply_markup"]) if k.startswith("set:")} == {
        data("page", name) for name in ("history", "behavior", "funding", "jobs", "personal")
    }
    for section, expected in [("history", "/memory"), ("behavior", "Be kind.")]:
        await tap(MEMBER, "page", section)
        assert expected in visible(page())
        assert set(buttons(page()["reply_markup"])) == {data("page", "main"), Dismiss().pack()}
        await tap(MEMBER, "page", "main")
    await tap(MEMBER, "page", "personal")
    await tap(ADMIN, "spend", "off")  # another viewer cannot act on the recipient’s keyboard
    assert conversation.api.calls_of("answerCallbackQuery")[-1]["show_alert"]
    await tap(MEMBER, "spend", "off")
    await tap(MEMBER, "named", "off")
    async with conversation.db.session() as session:
        user = await session.get(User, MEMBER.id)
        assert user and not user.patron_named and not user.group_spending
        chat = await session.get(Chat, CLUBHOUSE.id)
        assert chat and chat.ambient and chat.rules == "Be kind." and chat.retention_days is None

    panel = await open_panel(conversation, ADMIN, 502)
    for action, value in [
        ("page", "history"),
        ("page", "retention"),
        ("retention", "30"),
        ("page", "notes"),
        ("notes", "admins"),
    ]:
        await tap(ADMIN, action, value)
    assert "30 days" in visible(page()) and "/memory" in visible(page())
    for target in ("main", "behavior", "clear_rules"):
        await tap(ADMIN, "page", target)
    assert "Be kind." in visible(page()) and "pending" not in visible(page()).lower()
    async with conversation.db.session() as session:
        assert await session.scalar(select(Chat.rules).where(Chat.id == CLUBHOUSE.id)) == "Be kind."
    [confirm] = [k for k in buttons(page()["reply_markup"]) if k.startswith("set:rules:clear-")]
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, confirm)
    assert set(buttons(page()["reply_markup"])) == {data("page", "behavior"), Dismiss().pack()}
    for target in ("behavior", "main", "funding"):
        await tap(ADMIN, "page", target)
    after = conversation.seq
    await tap(ADMIN, "free", "on")
    [notice] = conversation.api.calls_of("sendRichMessage", after=after)
    assert not notice.fields.get("ephemeral_message_parameters")
    for target in ("main", "history", "ambient_off"):
        await tap(ADMIN, "page", target)
    assert "weren't addressed to me (**1**)" in visible(page())
    await tap(ADMIN, "ambient", "purge")
    assert set(buttons(page()["reply_markup"])) == {data("page", "history"), Dismiss().pack()}
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


async def test_browsing_reads_only_this_chats_funding_and_enabled_rules(conversation: Conversation) -> None:
    conversation.meet(MEMBER, CLUBHOUSE)
    await conversation.settle(await conversation.send(MEMBER, CLUBHOUSE, "hi"))
    async with conversation.db.transaction() as session:
        for kind, owner, amount in [
            ("chat", CLUBHOUSE.id, Decimal("2.75")),
            ("user", MEMBER.id, Decimal("19.25")),
        ]:
            await credit(
                session, (await wallet(session, kind, owner)).id, f"stars:{owner}", amount, "purchase"
            )
        for state in ("active", "active", "paused", "draft"):
            session.add(
                ReplyRule(
                    chat_id=CLUBHOUSE.id,
                    admin_id=ADMIN.id,
                    state=state,
                    condition="ask about dogs",
                    action="ask how the dog is doing",
                )
            )
        await session.execute(
            update(Chat).where(Chat.id == CLUBHOUSE.id).values(rules="<b>Keep it short</b>")
        )

    async def snapshot() -> tuple[Any, ...]:
        async with conversation.db.session() as session:
            chat = (
                await session.execute(
                    select(
                        Chat.ambient, Chat.retention_days, Chat.notes_mode, Chat.rules, Chat.free_models
                    ).where(Chat.id == CLUBHOUSE.id)
                )
            ).one()
            person = (
                await session.execute(
                    select(User.patron_named, User.group_spending, User.free_models_consent_version).where(
                        User.id == MEMBER.id
                    )
                )
            ).one()
            balances = list(await session.scalars(select(CreditLot.remaining).order_by(CreditLot.id)))
            tables = (Wallet, LedgerEntry, Reservation, ProviderCall, AgentRun, Job, ReplyRule)
            counts = [await session.scalar(select(func.count()).select_from(table)) for table in tables]
            return chat, person, balances, counts

    before = await snapshot()
    after = conversation.seq
    panel = await open_panel(conversation, MEMBER, 601)
    key = (CLUBHOUSE.id, panel)

    def screen() -> str:
        return visible(conversation.api.ephemerals[key]).replace("**", "")

    assert "2.75 dc" in screen() and "19.25" not in screen() and "rules enabled: 2" in screen().lower()
    for section in (
        "funding",
        "keys",
        "funding",
        "main",
        "behavior",
        "main",
        "history",
        "main",
        "jobs",
        "main",
        "personal",
    ):
        await conversation.press_ephemeral(MEMBER, *key, data("page", section))
        message = conversation.api.ephemerals[key]
        if section == "funding":
            assert "2.75 dc" in screen() and "19.25" not in screen()
            assert not any(k.startswith("set:free:") for k in buttons(message["reply_markup"]))
        elif section == "behavior":
            assert "<b>Keep it short</b>" in screen() and "Enabled: 2" in screen() and "Paused: 1" in screen()
    assert all(
        c.fields.get("ephemeral_message_parameters")
        for c in conversation.api.calls_of("sendRichMessage", after=after)
    )

    asked = await conversation.send(MEMBER, text="/settings")
    root = await conversation.expect_reply(asked)
    paid = root["rich_message"]["markdown"].replace("**", "")
    assert "19.25 dc" in paid and "2.75 dc" not in paid and "free today" in paid
    panel_id = max(mid for cid, mid in conversation.api.messages if cid == MEMBER.id)
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("page", "funding"))
    paid = visible(conversation.api.message(MEMBER.id, panel_id)).replace("**", "")
    assert "19.25 dc" in paid and f"{conversation.settings.sponsored_daily_credits} dc" in paid
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("page", "main"))
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("page", "personal"))
    assert data("spend", "off") in buttons(conversation.api.message(MEMBER.id, panel_id)["reply_markup"])
    assert await snapshot() == before


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
    assert data("page", "personal") in buttons(reply["reply_markup"])
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("page", "funding"))
    async with conversation.db.session() as session:
        assert await session.scalar(select(Wallet.id)) is None
        assert await session.scalar(select(CreditLot.id)) is None
        user = await session.get(User, MEMBER.id)
        assert user and user.free_models_consent_version is None
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("free", "on"))
    async with conversation.db.session() as session:
        user = await session.get(User, MEMBER.id)
        assert user is not None and user.free_models_consent_version == POLICY_VERSION
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("free", "off"))
    async with conversation.db.session() as session:
        user = await session.get(User, MEMBER.id)
        assert user is not None and user.free_models_consent_version is None


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

    zone = "Asia/Tokyo"
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == CLUBHOUSE.id).values(timezone=zone))
        next_at = await session.scalar(select(Job.next_at).where(Job.id == theirs))
    panel = await open_panel(conversation, MEMBER, 601)
    overview = visible(conversation.api.ephemerals[(CLUBHOUSE.id, panel)])
    assert next_at and next_at.astimezone(ZoneInfo(zone)).strftime("%H:%M") in overview and zone in overview
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("page", "jobs"))
    page = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]
    assert str(theirs) in page["rich_markdown"] and "daily" in page["rich_markdown"].lower()
    assert f"#{theirs}" not in page["rich_markdown"]  # "#" cites messages
    assert r"water the \*plants\* \<b\>now\<\/b\>" in page["rich_markdown"]  # shown literally
    assert set(buttons(page["reply_markup"])) == {
        data("jpause", str(mine)), data("jdrop", str(mine)), data("page", "main"), Dismiss().pack()
    }  # fmt: skip
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("jpause", str(mine)))
    async with conversation.db.session() as session:
        assert (await session.get(Job, mine)).state == "paused"  # type: ignore[union-attr]
    shown = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]
    assert "paused" in shown["rich_markdown"]
    assert data("jdrop", str(mine)) not in buttons(shown["reply_markup"])
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("page", "jobs"))
    await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, panel, data("jresume", str(mine)))
    async with conversation.db.session() as session:
        assert (await session.get(Job, mine)).state == "active"  # type: ignore[union-attr]

    admin_panel = await open_panel(conversation, ADMIN, 602)
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
    first_receipt = conversation.api.ephemerals[(CLUBHOUSE.id, admin_panel)]
    assert "cancelled" in first_receipt["rich_markdown"] and not first_receipt.get("reply_markup")
    admin_panel = await open_panel(conversation, ADMIN, 603)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("page", "jobs"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin_panel, data("jdrop", str(theirs)))
    assert not conversation.api.ephemerals[(CLUBHOUSE.id, admin_panel)].get("reply_markup")


async def test_a_job_of_another_chat_is_never_touched(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("schedule", text="mine", in_minutes=60), ".")
    await conversation.settle(await conversation.send(MEMBER, text="remind me"))
    async with conversation.db.session() as session:
        job_id = await session.scalar(select(Job.id))
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    asked = await conversation.send(MEMBER, text="/settings")
    await conversation.expect_reply(asked)
    panel_id = max(mid for (cid, mid) in conversation.api.messages if cid == MEMBER.id)
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("page", "jobs"))
    async with conversation.db.transaction() as session:
        await session.execute(update(Job).values(chat_id=CLUBHOUSE.id))
    await conversation.press(MEMBER, MEMBER.id, panel_id, data("jdrop", str(job_id)))
    assert "isn't scheduled" in conversation.api.calls_of("answerCallbackQuery")[-1]["text"]
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.state)) == "active"


async def test_member_reads_full_standing_instructions_and_admin_reviews_the_exact_removal(
    conversation: Conversation,
) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    conversation.meet(MEMBER, CLUBHOUSE)
    await conversation.settle(await conversation.send(MEMBER, CLUBHOUSE, "hi"))
    rules = "Keep <tags>, literal **stars** & quotes. " * 19 + "Last instruction."
    async with conversation.db.transaction() as session:
        await session.execute(
            update(Chat)
            .where(Chat.id == CLUBHOUSE.id)
            .values(rules=rules, rules_draft="Keep the new proposal.", rules_draft_by=ADMIN.id)
        )
    member = await open_panel(conversation, MEMBER, 801)

    async def member_reads(section: str) -> dict[str, Any]:
        await conversation.press_ephemeral(MEMBER, CLUBHOUSE.id, member, data("page", section))
        return conversation.api.ephemerals[CLUBHOUSE.id, member]

    summary = await member_reads("behavior")
    assert "Last instruction." not in visible(summary)
    assert set(buttons(summary["reply_markup"])) == {
        data("page", "instructions"),
        data("page", "main"),
        Dismiss().pack(),
    }
    full = await member_reads("instructions")
    assert rules in visible(full)
    assert set(buttons(full["reply_markup"])) == {data("page", "behavior"), Dismiss().pack()}
    assert data("page", "instructions") in buttons((await member_reads("behavior"))["reply_markup"])
    admin = await open_panel(conversation, ADMIN, 802)
    for section in ("behavior", "clear_rules"):
        await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin, data("page", section))
    preview = visible(conversation.api.ephemerals[CLUBHOUSE.id, admin])
    assert rules in preview and "pending" in preview.lower()
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, admin, data("page", "behavior"))
    async with conversation.db.session() as session:
        chat = await session.get(Chat, CLUBHOUSE.id)
        assert chat and chat.rules == rules and chat.rules_draft == "Keep the new proposal."


async def test_old_clear_button_reviews_and_a_stale_confirmation_preserves_the_new_rules(
    conversation: Conversation,
) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    await conversation.settle(await conversation.send(ADMIN, CLUBHOUSE, "hi"))
    async with conversation.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == CLUBHOUSE.id).values(rules="Use plain text."))
    assert conversation.services
    action = SettingsAction(action="rules", value="clear")
    legacy = await conversation.services.bot.send_message(
        CLUBHOUSE.id, "Chat settings", reply_markup=markup(button("Clear instructions", action))
    )
    mid = legacy.message_id
    await conversation.press(ADMIN, CLUBHOUSE.id, mid, action.pack())
    confirmation = conversation.api.message(CLUBHOUSE.id, mid)
    [confirm] = [k for k in buttons(confirmation["reply_markup"]) if k.startswith("set:rules:clear-")]
    async with conversation.db.transaction() as session:
        chat = await session.get(Chat, CLUBHOUSE.id)
        assert chat and chat.rules == "Use plain text."
        chat.rules = "Answer in German."
    await conversation.press(ADMIN, CLUBHOUSE.id, mid, confirm)
    receipt = conversation.api.message(CLUBHOUSE.id, mid)
    assert "changed" in visible(receipt) and confirm not in buttons(receipt["reply_markup"])
    await conversation.press(ADMIN, CLUBHOUSE.id, mid, data("page", "behavior"))
    assert "Answer in German." in visible(conversation.api.message(CLUBHOUSE.id, mid))


async def test_retention_offers_configured_days_and_shortening_asks_first(conversation: Conversation) -> None:
    conversation.meet(ADMIN, CLUBHOUSE, status="administrator")
    panel = await open_panel(conversation, ADMIN, 701)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "history"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "retention"))
    offered = buttons(conversation.api.ephemerals[(CLUBHOUSE.id, panel)]["reply_markup"])
    days = conversation.settings.retention.options
    assert [d for d in offered if d.startswith("set:retention:") and d != data("retention", "0")] == [
        data("retention", str(d)) for d in days
    ]
    shorter = min(days)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("retention", str(shorter)))
    preview = visible(conversation.api.ephemerals[(CLUBHOUSE.id, panel)])
    assert (
        f"older than {shorter} day" in preview
        and f"{conversation.settings.retention.group_days} days" in preview
    )
    async with conversation.db.session() as session:
        assert (await session.get(Chat, CLUBHOUSE.id)).retention_days is None  # type: ignore[union-attr]
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("shorten", str(shorter)))
    async with conversation.db.session() as session:
        assert (await session.get(Chat, CLUBHOUSE.id)).retention_days == shorter  # type: ignore[union-attr]
    receipt = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]
    assert set(buttons(receipt["reply_markup"])) == {data("page", "history"), Dismiss().pack()}
    text = visible(receipt)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, Dismiss().pack())
    closed = conversation.api.ephemerals[(CLUBHOUSE.id, panel)]
    assert visible(closed) == text and not closed.get("reply_markup")
    panel = await open_panel(conversation, ADMIN, 702)
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "history"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("page", "retention"))
    await conversation.press_ephemeral(ADMIN, CLUBHOUSE.id, panel, data("retention", str(max(days))))
    async with conversation.db.session() as session:  # lengthening applies at once
        assert (await session.get(Chat, CLUBHOUSE.id)).retention_days == max(days)  # type: ignore[union-attr]
