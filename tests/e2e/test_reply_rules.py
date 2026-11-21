"""A group admin installs an automatic reply, a member triggers it, the group pays."""

from typing import Any

from sqlalchemy import select

from derp.agent.models import AgentRun, Approval
from derp.memory.models import ReplyRule, ReplyRuleCheck
from derp.money.wallets import credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_decisions import EASY, noul, with_jev
from tests.e2e.test_effects import results_of

ADMIN = Person(965, "Jo", "jo_rules")
ALEX = Person(966, "Alex", "alex_rules")
GROUP = Room(-10965, title="Discussion", is_forum=True)
TOPIC = 12


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
        "see card",
    )
    sent = await conversation.send(
        ADMIN, GROUP, "derp reply to Alex when he mentions his dog", thread_id=TOPIC
    )
    card = await conversation.expect_reply(sent)
    await conversation.settle(sent)
    async with conversation.db.session() as s:
        rule = (await s.scalars(select(ReplyRule))).one()
        assert (rule.state, rule.sender_id, rule.thread_id) == ("draft", ALEX.id, TOPIC)
    assert "each eligible message" in card["rich_message"]["markdown"]
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
    from datetime import UTC, datetime

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
        "see card",
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
        await conversation.press(actor, GROUP.id, mid, f"autoreply:{op}:{draft.id}")

    await press("all")
    async with conversation.db.session() as s:
        active = await s.get(ReplyRule, draft.id)
        assert active and active.whole_chat and active.sender_id is None
        assert await s.get(ReplyRule, old) is None
    for op, state, actor in [
        ("pause", "active", ALEX),
        ("pause", "paused", ADMIN),
        ("resume", "active", ADMIN),
    ]:
        await press(op, actor)
        async with conversation.db.session() as s:
            active = await s.get(ReplyRule, draft.id)
            assert active and active.state == state
    await press("remove")
    async with conversation.db.session() as s:
        assert await s.get(ReplyRule, draft.id) is None
