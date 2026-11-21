"""A group admin installs an automatic reply, a member triggers it, the group pays."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from derp.agent.models import AgentRun, Approval
from derp.history.models import Chat
from derp.locales import t
from derp.memory.models import ReplyRule, ReplyRuleCheck
from derp.memory.reply_rules import PAGE_SIZE, RuleButton
from derp.money.wallets import credit, wallet
from derp.settings.panel import SettingsAction
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_decisions import EASY, noul, with_jev
from tests.e2e.test_effects import results_of

ADMIN = Person(965, "Jo", "jo_rules")
ALEX = Person(966, "Alex", "alex_rules")
GROUP = Room(-10965, title="Discussion", is_forum=True)
TOPIC = 12


def buttons(message: Mapping[str, Any]) -> dict[tuple[str, int], dict[str, str]]:
    controls = {}
    for row in (message.get("reply_markup") or {}).get("inline_keyboard", []):
        for button in row:
            if (payload := button.get("callback_data", "")).startswith("autoreply:"):
                decoded = RuleButton.unpack(payload)
                controls[decoded.op, decoded.id] = button
    return controls


def card_text(message: Mapping[str, Any]) -> str:
    return "\n\n".join(block["text"] for block in message["rich_message"]["blocks"]).replace("\\_", "_")


async def fund(conversation: Conversation, kind: str = "chat") -> None:
    async with conversation.db.transaction() as session:
        payer = await wallet(session, kind, GROUP.id if kind == "chat" else ALEX.id)
        await credit(session, payer.id, f"test:{kind}", 1000, "purchase")


async def seed(conversation: Conversation, **overrides: Any) -> int:
    conversation.meet(ADMIN, GROUP, status="administrator")
    conversation.meet(ALEX, GROUP)
    await conversation.settle(await conversation.send(ADMIN, GROUP, "hello", thread_id=TOPIC))
    async with conversation.db.transaction() as session:
        rule = ReplyRule(
            **(
                {
                    "chat_id": GROUP.id,
                    "thread_id": TOPIC,
                    "admin_id": ADMIN.id,
                    "sender_id": ALEX.id,
                    "condition": "about dogs",
                    "action": "ask about the dog",
                    "state": "active",
                }
                | overrides
            )
        )
        session.add(rule)
        await session.flush()
        return rule.id


async def test_admin_confirms_scope_and_distinct_matches_reply_in_order(
    conversation: Conversation,
    model: ScriptedModel,
) -> None:
    calls: dict[str, Any] = {"effort": EASY}
    jev = await with_jev(
        conversation,
        lambda line: {k: v if k == "effort" or "dog" in line else noul(0) for k, v in calls.items()},
    )
    await fund(conversation)
    conversation.meet(ADMIN, GROUP, status="administrator")
    await conversation.settle(await conversation.send(ALEX, GROUP, "hello", thread_id=TOPIC))
    model.reply(
        tool_call(
            "auto_reply",
            operation="create",
            sender="@alex_rules",
            condition="Mentions their dog",
            action="Ask how the dog is doing.",
        ),
    )
    sent = await conversation.send(
        ADMIN, GROUP, "derp reply to Alex when he mentions his dog", thread_id=TOPIC
    )
    card = await conversation.expect_reply(sent)
    await conversation.settle(sent)
    async with conversation.db.session() as s:
        rule = (await s.scalars(select(ReplyRule))).one()
        assert (rule.state, rule.sender_id, rule.thread_id) == ("draft", ALEX.id, TOPIC)
    assert "dc" in (card["rich_message"].get("html") or card["rich_message"]["markdown"])
    card_id = next(
        mid
        for (cid, mid), msg in conversation.api.messages.items()
        if cid == GROUP.id and "autoreply:topic" in str(msg.get("reply_markup"))
    )
    enable = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await conversation.press(ALEX, GROUP.id, card_id, enable)
    await conversation.press(ADMIN, GROUP.id, card_id, enable)
    extra = await seed(conversation)
    calls.update({f"rule_{rule.id}": noul(1), f"rule_{extra}": noul(1)})
    for who, topic, text in [
        (ADMIN, TOPIC, "dog"),
        (ALEX, TOPIC + 1, "dog"),
        (Person(967, "Bot", is_bot=True), TOPIC, "derp dog"),
    ]:
        await conversation.settle(await conversation.send(who, GROUP, text, thread_id=topic))
    miss = await conversation.send(ALEX, GROUP, "the weather", thread_id=TOPIC)
    await conversation.settle(miss)
    model.reply("one", "two", "three")
    sent_dogs = [
        await conversation.send(ALEX, GROUP, f"my dog number {n}", thread_id=TOPIC) for n in range(3)
    ]
    for n, dog in enumerate(sent_dogs):
        assert (await conversation.expect_reply(dog))["rich_message"]["markdown"] == ["one", "two", "three"][
            n
        ]
        await conversation.settle(dog)
    await conversation.settle(await conversation.push("message", sent_dogs[0].message))
    await conversation.settle(
        await conversation.push("edited_message", {**sent_dogs[0].message, "text": "edited dog"})
    )
    checks = [qs for _, qs in jev.asked if any(q.startswith("rule_") for q in qs)]
    assert checks == [[f"rule_{rule.id}", f"rule_{extra}"]] * 4
    async with conversation.db.session() as s:
        assert list(
            await s.scalars(
                select(AgentRun.trigger_message_id).where(AgentRun.trigger == "rule").order_by(AgentRun.id)
            )
        ) == [item.message_id for item in sent_dogs]
        recorded = await s.get(ReplyRuleCheck, (GROUP.id, miss.message_id))
        assert recorded is not None and recorded.matched == []


async def test_screening_never_spends_a_members_wallet(
    conversation: Conversation, model: ScriptedModel
) -> None:
    jev = await with_jev(conversation, lambda _line: {})
    await seed(conversation)
    await fund(conversation, "user")
    sent = await conversation.send(ALEX, GROUP, "a dog", thread_id=TOPIC)
    await conversation.settle(sent)
    assert not jev.asked and not model.requests
    async with conversation.db.session() as session:
        assert await session.get(ReplyRuleCheck, (GROUP.id, sent.message_id)) is not None
        assert not list(await session.scalars(select(AgentRun)))


async def test_additional_side_effect_requires_its_admin_and_exact_call(
    conversation: Conversation,
    model: ScriptedModel,
) -> None:
    qs = {"effort": EASY}
    await with_jev(conversation, lambda _line: qs)
    rid = await seed(conversation)
    qs[f"rule_{rid}"] = noul(1)
    await fund(conversation)
    model.reply(tool_call("remember", text="The dog is named Pepper", about="chat"))
    sent = await conversation.send(ALEX, GROUP, "my dog", thread_id=TOPIC)
    card = await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert "Pepper" in card["rich_message"]["markdown"]
    async with conversation.db.session() as session:
        approval = (await session.scalars(select(Approval))).one()
        assert approval.kind == "rule_action" and approval.user_id == ADMIN.id and approval.prompt_message_id
    confirm = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await conversation.press(ALEX, GROUP.id, approval.prompt_message_id, confirm)
    model.reply("saved")
    await conversation.press(ADMIN, GROUP.id, approval.prompt_message_id, confirm)
    assert any("Saved as note" in part for part in results_of(model, "remember"))


async def test_exact_schedule_grant_inherits_authority_when_the_job_fires(
    conversation: Conversation, model: ScriptedModel
) -> None:
    from derp.scheduler.models import Job
    from tests.e2e.test_scheduler import fire_now

    qs = {"effort": EASY}
    await with_jev(conversation, lambda _line: qs)
    args = {"text": "fetch fresh market data", "in_minutes": 1, "kind": "prompt"}
    rid = await seed(conversation, grants=[{"tool": "schedule", "arguments": args}])
    qs[f"rule_{rid}"] = noul(1)
    await fund(conversation)
    model.reply(tool_call("schedule", **args), "Scheduled")
    sent = await conversation.send(ALEX, GROUP, "my dog", thread_id=TOPIC)
    await conversation.settle(sent)
    async with conversation.db.session() as s:
        job = await s.scalar(select(Job))
        assert job is not None, (
            [row.args for row in await s.scalars(select(Approval))],
            results_of(model, "schedule"),
        )
        assert (job.creator_id, job.rule_id) == (ADMIN.id, rid)
        assert not list(await s.scalars(select(Approval)))
    model.reply(tool_call("remember", text="Markets closed", about="chat"))
    assert await fire_now(conversation) == 1
    async with conversation.db.session() as s:
        approval = (await s.scalars(select(Approval))).one()
        assert approval.kind == "rule_action" and approval.user_id == ADMIN.id and approval.prompt_message_id
    current = conversation.api.message(GROUP.id, approval.prompt_message_id)
    confirm = current["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    model.reply("Stored")
    await conversation.press(ADMIN, GROUP.id, approval.prompt_message_id, confirm)
    assert any("Saved as note" in part for part in results_of(model, "remember"))


async def test_a_visual_rule_screens_the_stored_reply_photo_but_skips_a_forgotten_one(
    conversation: Conversation,
) -> None:
    from derp.history.retention import forget_message
    from tests.e2e.test_media import PNG, photo

    qs = {"effort": EASY}
    jev = await with_jev(conversation, lambda _line: qs)
    rid = await seed(conversation, needs_media=True)
    qs[f"rule_{rid}"] = noul(0)
    await fund(conversation)
    conversation.api.register_file("dog_source", PNG)
    posted = await conversation.send(ADMIN, GROUP, thread_id=TOPIC, photo=photo("dog_source"))
    await conversation.settle(posted)
    for before_forget in [True, False]:
        if not before_forget:
            async with conversation.db.transaction() as session:
                await forget_message(session, GROUP.id, posted.message_id, datetime.now(UTC))
        asked = await conversation.send(ALEX, GROUP, "a dog?", thread_id=TOPIC, reply_to=posted.message_id)
        await conversation.settle(asked)
    received = jev.images
    assert len(received) == 2 and received[0] and received[0].startswith(b"\xff\xd8") and received[1] is None


async def test_rule_revision_replaces_only_on_confirmation_and_obeys_management_controls(
    conversation: Conversation,
    model: ScriptedModel,
) -> None:
    old = await seed(conversation)
    await fund(conversation)
    model.reply(
        tool_call(
            "auto_reply",
            operation="revise",
            rule_id=old,
            sender="*",
            condition="mentions gold prices",
            action="get fresh prices",
        ),
    )
    sent = await conversation.send(ADMIN, GROUP, "derp revise the rule", thread_id=TOPIC)
    await conversation.settle(sent)
    async with conversation.db.session() as s:
        previous = await s.get(ReplyRule, old)
        assert previous and previous.state == "active"
        draft = (await s.scalars(select(ReplyRule).where(ReplyRule.replaces_id == old))).one()
        assert draft.state == "draft"
    mid = next(
        mid
        for (cid, mid), msg in conversation.api.messages.items()
        if cid == GROUP.id and "autoreply:all" in str(msg.get("reply_markup"))
    )

    async def press(op: str, actor: Person = ADMIN) -> None:
        controls = buttons(conversation.api.message(GROUP.id, mid))
        await conversation.press(actor, GROUP.id, mid, controls[op, draft.id]["callback_data"])

    await press("all")
    async with conversation.db.session() as s:
        active = await s.get(ReplyRule, draft.id)
        assert active and active.whole_chat and active.sender_id is None
        assert await s.get(ReplyRule, old) is None
    await press("view")
    for op, state, actor in [
        ("pause", "active", ALEX),
        ("pause", "paused", ADMIN),
        ("resume", "active", ADMIN),
    ]:
        await press(op, actor)
        async with conversation.db.session() as s:
            active = await s.get(ReplyRule, draft.id)
            assert active and active.state == state
        if actor == ADMIN:
            assert set(buttons(conversation.api.message(GROUP.id, mid))) == {
                ("ok", draft.id),
                ("view", draft.id),
            }
            await press("view")
    await press("remove")
    assert "reply_markup" not in conversation.api.message(GROUP.id, mid)
    async with conversation.db.session() as s:
        assert await s.get(ReplyRule, draft.id) is None


async def test_russian_public_card_keeps_its_language_and_admin_dismiss_keeps_its_receipt(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await fund(conversation)
    conversation.meet(ADMIN, GROUP, status="administrator")
    conversation.meet(ALEX, GROUP)
    await conversation.settle(await conversation.send(ALEX, GROUP, "hello", thread_id=TOPIC))
    condition = "gold price < 3500.50"
    action = "покажи график цены"
    reminder = "проверить S&P 500 завтра"
    model.reply(
        tool_call(
            "auto_reply",
            operation="create",
            condition=condition,
            action=action,
            sender="@alex_rules",
            outputs=["market_chart"],
            calls=[{"tool": "schedule", "arguments": {"text": reminder, "in_minutes": 10}}],
        ),
        "Всё включено. Ничего нажимать не нужно.",
    )
    sent = await conversation.send(
        ADMIN,
        GROUP,
        f"Дерп, когда Алекс пишет «{condition}», покажи график цены. "
        "И поставь напоминание через десять минут.",
        thread_id=TOPIC,
    )
    await conversation.settle(sent)
    # Onboarding may announce the bot to the group independently of its reply to this request.
    replies = [
        msg
        for (chat_id, mid), msg in conversation.api.messages.items()
        if chat_id == GROUP.id and mid > sent.message_id and msg["from"]["is_bot"]
    ]
    assert len(model.requests) == 1
    assert not any("Ничего нажимать не нужно" in str(msg) for msg in replies)
    [draft] = [
        msg for msg in replies if (msg.get("reply_to_message") or {}).get("message_id") == sent.message_id
    ]
    mid = draft["message_id"]
    async with conversation.db.session() as session:
        rule = (await session.scalars(select(ReplyRule))).one()
        assert rule.state == "draft" and rule.sender_id == ALEX.id
    text = card_text(draft)
    assert t("ru", "auto.draft", id=rule.id) in text
    assert all(piece in text for piece in [condition, action, "@alex_rules", reminder])
    assert "market_chart" not in text and '"in_minutes"' not in text
    assert "0.0504" in text.replace(",", ".")
    controls = buttons(draft)
    for op in ("topic", "all", "cancel"):
        assert controls[op, rule.id]["text"] == t("ru", f"auto.{op}")
    # Public copy belongs to the original request, although the admin's Telegram client is English.
    await conversation.press(ADMIN, GROUP.id, mid, controls["topic", rule.id]["callback_data"])
    receipt = conversation.api.message(GROUP.id, mid)
    assert all(piece in card_text(receipt) for piece in [condition, action, "@alex_rules"])
    controls = buttons(receipt)
    assert set(controls) == {("ok", rule.id), ("view", rule.id)}
    assert t("ru", "auto.heading_active", id=rule.id) in card_text(receipt)
    assert controls["view", rule.id]["text"] == t("ru", "auto.view")
    assert controls["ok", rule.id]["text"] == t("ru", "auto.ok")
    ok = controls["ok", rule.id]["callback_data"]
    await conversation.press(ALEX, GROUP.id, mid, ok)
    assert conversation.api.message(GROUP.id, mid) == receipt
    before = conversation.seq
    conversation.api.set_member_status(chat_id=GROUP.id, user_id=ALEX.id, status="administrator")
    await conversation.press(ALEX, GROUP.id, mid, ok)
    dismissed = conversation.api.message(GROUP.id, mid)
    assert "reply_markup" not in dismissed
    assert dismissed["rich_message"] == receipt["rich_message"]
    assert len(conversation.api.calls_of("editMessageReplyMarkup", after=before)) == 1
    assert not conversation.api.calls_of("editMessageText", after=before)
    async with conversation.db.session() as session:
        kept = await session.get(ReplyRule, rule.id)
        assert kept and kept.state == "active" and not kept.whole_chat and kept.thread_id == TOPIC


async def test_a_preupgrade_public_pause_button_updates_the_original_rule_and_keeps_controls(
    conversation: Conversation,
) -> None:
    rid = await seed(conversation, sender_name="Alex")
    # The payload and old card text are literal: newer encoders must not rewrite the legacy fixture.
    mid = conversation.api.take_message_id(GROUP.id)
    old = {
        "message_id": mid,
        "date": 1,
        "chat": GROUP.payload(),
        "from": conversation.api.bot_user,
        "text": f"Rule {rid} — Active",
        "reply_markup": {"inline_keyboard": [[{"text": "Pause", "callback_data": f"autoreply:pause:{rid}"}]]},
    }
    conversation.api.register_message(old)
    russian_client = Person(ADMIN.id, ADMIN.first_name, ADMIN.username, language_code="ru")
    await conversation.press(russian_client, GROUP.id, mid, f"autoreply:pause:{rid}")
    detail = conversation.api.message(GROUP.id, mid)
    controls = buttons(detail)
    assert set(controls) == {("ok", rid), ("view", rid)}
    await conversation.press(ADMIN, GROUP.id, mid, controls["view", rid]["callback_data"])
    controls = buttons(conversation.api.message(GROUP.id, mid))
    assert controls["resume", rid]["text"] == t("en", "auto.resume")
    async with conversation.db.session() as session:
        paused = await session.get(ReplyRule, rid)
        assert paused and paused.state == "paused"
    await conversation.press(ADMIN, GROUP.id, mid, controls["resume", rid]["callback_data"])
    assert set(buttons(conversation.api.message(GROUP.id, mid))) == {("ok", rid), ("view", rid)}


async def test_legacy_back_uses_the_original_request_language_for_the_shared_list(
    conversation: Conversation,
) -> None:
    rid = await seed(conversation)
    original = await conversation.send(ADMIN, GROUP, "изменить правило для этой темы", thread_id=TOPIC)
    await conversation.settle(original)
    mid = conversation.api.take_message_id(GROUP.id)
    legacy = "autoreply:previous:0"
    conversation.api.register_message(
        {
            "message_id": mid,
            "date": 1,
            "chat": GROUP.payload(),
            "from": conversation.api.bot_user,
            "reply_to_message": original.message,
            "text": "Старое правило",
            "reply_markup": {"inline_keyboard": [[{"text": "Назад", "callback_data": legacy}]]},
        }
    )
    await conversation.press(ADMIN, GROUP.id, mid, legacy)
    listing = conversation.api.message(GROUP.id, mid)
    assert t("ru", "auto.panel") in card_text(listing)
    assert buttons(listing)["view", rid]["text"] == t("ru", "auto.manage", id=rid)


async def test_settings_lists_rules_across_pages_and_opens_the_selected_rule(
    conversation: Conversation,
) -> None:
    first = await seed(conversation)
    async with conversation.db.transaction() as session:
        extra = [
            ReplyRule(
                chat_id=GROUP.id,
                admin_id=ADMIN.id,
                whole_chat=True,
                state="active",
                condition=f"market {n}",
                action=f"quoted price {n}",
                grants=[{"tool": "market_chart"}],
            )
            for n in range(1, PAGE_SIZE + 1)
        ]
        session.add_all(extra)
        await session.flush()
        last = extra[-1].id

    await conversation.settle(await conversation.ephemeral_command(ADMIN, GROUP, "/settings", 501))
    key = max(k for k in conversation.api.ephemerals if k[0] == GROUP.id)
    await conversation.press_ephemeral(ADMIN, *key, SettingsAction(action="page", value="auto").pack())

    def page_buttons() -> dict[tuple[str, int], dict[str, str]]:
        return buttons(conversation.api.ephemerals[key])

    def visible() -> set[int]:
        return {rid for op, rid in page_buttons() if op == "view"}

    assert first in visible() and len(visible()) == PAGE_SIZE and last not in visible()
    await conversation.press_ephemeral(ADMIN, *key, page_buttons()["next", PAGE_SIZE]["callback_data"])
    assert visible() == {last}
    await conversation.press_ephemeral(ADMIN, *key, page_buttons()["previous", 0]["callback_data"])
    assert first in visible() and last not in visible()
    await conversation.press_ephemeral(ADMIN, *key, page_buttons()["next", PAGE_SIZE]["callback_data"])
    conversation.api.fail_next(
        "editEphemeralMessageText", error_code=400, description="Bad Request: can't parse rich message"
    )
    await conversation.press_ephemeral(ADMIN, *key, page_buttons()["view", last]["callback_data"])
    details = conversation.api.ephemerals[key]["text"]
    assert "rich_message" not in conversation.api.ephemerals[key]
    assert f"market {PAGE_SIZE}" in details and f"quoted price {PAGE_SIZE}" in details
    assert "market_chart" not in details
    assert ("pause", last) in page_buttons()
    before = conversation.seq
    await conversation.press_ephemeral(ADMIN, *key, page_buttons()["ok", last]["callback_data"])
    assert conversation.api.ephemerals[key]["text"] == details and not page_buttons()
    assert len(conversation.api.calls_of("editEphemeralMessageReplyMarkup", after=before)) == 1


async def test_refused_rich_draft_falls_back_to_literal_text_and_working_controls(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(ADMIN, GROUP, status="administrator")
    await conversation.settle(await conversation.send(ADMIN, GROUP, "hello"))
    async with conversation.db.transaction() as session:
        chat = await session.get(Chat, GROUP.id)
        assert chat
        chat.onboarded_at = datetime.now(UTC)
    await fund(conversation)
    condition = "gold < 3500.50 & silver > 42"
    model.reply(
        tool_call(
            "auto_reply",
            operation="create",
            sender="*",
            condition=condition,
            action="reply with the current price",
        )
    )
    conversation.api.fail_next(
        "sendRichMessage", error_code=400, description="Bad Request: invalid rich text"
    )
    sent = await conversation.send(ADMIN, GROUP, "derp, reply to gold and silver quotes", thread_id=TOPIC)
    await conversation.settle(sent)
    plain = await conversation.expect_reply(sent, method="sendMessage")
    assert condition in plain["text"] and "<p>" not in plain["text"] and "&lt;" not in plain["text"]
    async with conversation.db.session() as session:
        [rule] = list(await session.scalars(select(ReplyRule)))
        assert rule.state == "draft"
    (mid,) = (
        mid
        for (cid, mid), msg in conversation.api.messages.items()
        if cid == GROUP.id and ("topic", rule.id) in buttons(msg)
    )
    before = conversation.seq
    conversation.api.fail_next(
        "editMessageText", error_code=400, description="Bad Request: can't parse rich message"
    )
    await conversation.press(ADMIN, GROUP.id, mid, buttons(plain.fields)["topic", rule.id]["callback_data"])
    rich_edit, plain_edit = conversation.api.calls_of("editMessageText", after=before)
    assert rich_edit["rich_message"].get("html") and condition in plain_edit["text"]
    assert plain_edit["link_preview_options"]["is_disabled"]
    receipt = conversation.api.message(GROUP.id, mid)
    assert receipt["text"] == plain_edit["text"] and ("ok", rule.id) in buttons(receipt)
    async with conversation.db.session() as session:
        stored = await session.get(ReplyRule, rule.id)
        assert stored and stored.state == "active"
