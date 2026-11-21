"""A delivered rule draft answers a standalone request. A second task can still get its own answer."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from derp.agent.models import AgentRun, Approval, RunOutput
from derp.history.models import Chat
from derp.inference.models import ProviderCall
from derp.memory.models import ReplyRule
from tests.e2e.harness import Conversation
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_decisions import EASY, noul, with_jev
from tests.e2e.test_effects import results_of
from tests.e2e.test_reply_rules import ADMIN, ALEX, GROUP, TOPIC, fund, seed


@pytest.mark.parametrize("combined", [False, True], ids=["rule-only", "also-needs-an-answer"])
async def test_confirming_rule_only_calls_the_model_again_for_an_independent_request(
    conversation: Conversation, model: ScriptedModel, combined: bool
) -> None:
    await fund(conversation)
    conversation.meet(ADMIN, GROUP, status="administrator")
    await conversation.settle(await conversation.send(ADMIN, GROUP, text="hello", thread_id=TOPIC))
    async with conversation.db.transaction() as session:
        group = await session.get(Chat, GROUP.id)
        assert group
        group.onboarded_at = datetime.now(UTC)
    extra = {"more_requested_work": True} if combined else {}
    model.reply(
        tool_call(
            "auto_reply",
            sender="*",
            condition="asks about dinner",
            action="Share the menu.",
            **extra,
        ),
        "Four." if combined else "I already enabled the rule.",
    )
    ask = "Derp, reply with the menu when anyone asks about dinner."
    if combined:
        ask += " Also, what is two plus two?"
    sent = await conversation.send(ADMIN, GROUP, text=ask, thread_id=TOPIC)
    await conversation.settle(sent)
    replies = [
        message
        for (chat_id, mid), message in sorted(conversation.api.messages.items())
        if chat_id == GROUP.id and mid > sent.message_id and message["from"]["is_bot"]
    ]
    assert "reply_markup" in replies[0]
    expected_calls = 2 if combined else 1
    assert len(model.requests) == len(replies) == expected_calls
    if combined:
        closing = conversation.api.calls_of("sendRichMessage", after=sent.after)[-1]
        assert closing["rich_message"]["markdown"] == "Four."
        assert "reply_markup" not in closing.fields
    async with conversation.db.session() as session:
        run = (await session.scalars(select(AgentRun))).one()
        assert run.status == "completed"
        assert (
            len(list(await session.scalars(select(ProviderCall).where(ProviderCall.run_id == run.id))))
            == expected_calls
        )
        assert (
            len(list(await session.scalars(select(RunOutput).where(RunOutput.run_id == run.id))))
            == expected_calls
        )
        assert run.messages and sum(m["kind"] == "response" for m in run.messages) == expected_calls
        assert any(
            p.get("part_kind") == "tool-return" and "Shown for confirmation" in str(p.get("content"))
            for m in run.messages
            for p in m["parts"]
        )


async def test_group_admin_manages_installed_rule_and_an_automatic_call_needs_approval(
    conversation: Conversation, model: ScriptedModel
) -> None:
    responses = {"effort": EASY}
    await with_jev(conversation, lambda _line: responses)
    rule_id = await seed(conversation)
    responses[f"rule_{rule_id}"] = noul(1)
    await fund(conversation)

    model.reply(tool_call("manage_auto_reply", operation="list"), "Here's the rule.")
    sent = await conversation.send(ADMIN, GROUP, text="Derp, list our reply rules", thread_id=TOPIC)
    await conversation.settle(sent)
    assert any("dog" in entry for entry in results_of(model, "manage_auto_reply"))
    for operation, state in [("pause", "paused"), ("resume", "active")]:
        model.reply(tool_call("manage_auto_reply", operation=operation, rule_id=rule_id), "Done.")
        sent = await conversation.send(
            ADMIN, GROUP, text=f"Derp, {operation} rule {rule_id}", thread_id=TOPIC
        )
        await conversation.settle(sent)
        async with conversation.db.session() as session:
            row = await session.get(ReplyRule, rule_id)
            assert row and row.state == state

    model.reply(tool_call("manage_auto_reply", operation="remove", rule_id=rule_id))
    sent = await conversation.send(ALEX, GROUP, "my dog", thread_id=TOPIC)
    await conversation.settle(sent)
    async with conversation.db.session() as session:
        row = await session.get(ReplyRule, rule_id)
        approval = (await session.scalars(select(Approval))).one()
        assert row and row.state == "active"
        assert approval.kind == "rule_action" and approval.user_id == ADMIN.id
        assert approval.args["job"]["rule_tool"] == "manage_auto_reply"
