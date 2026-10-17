from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from derp.billing.models import Purchase
from derp.history.models import Chat, Message
from derp.inference.models import ModelPin, ProviderCall, ScoutProposal
from derp.money.wallets import balance, wallet
from derp.ops.console import ConsoleAction, proposal_buttons
from derp.ops.owner import notify_owners
from derp.scheduler.models import Job
from derp.scheduler.worker import run_due
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

OWNER = Person(1000, "Olga")
PAT = Person(1101, "Pat")
YARD = Room(-1040, title="Yard")


@dataclass
class FakeKeyAPI:
    """Stands in for OpenRouterAPI: a key with a spending limit."""

    async def key_info(self) -> dict[str, Any]:
        return {"limit": 50, "limit_remaining": 12.5, "usage": 37.5}

    async def close(self) -> None:
        return None


async def test_stats_show_spend_revenue_and_decide_proposals(conversation: Conversation) -> None:
    conversation.overrides["api"] = FakeKeyAPI()
    await conversation.restart()
    conversation.meet(PAT, YARD)
    await conversation.settle(await conversation.send(PAT, YARD, "hello yard"))
    async with conversation.db.transaction() as session:
        session.add_all(
            [
                ProviderCall(
                    kind="chat", model="a/b", chat_id=YARD.id, funding="paid", cost_usd=Decimal("0.30")
                ),
                ProviderCall(kind="stt", model="a/c", funding="creator", cost_usd=Decimal("0.02")),
                Purchase(
                    telegram_charge_id="ch1",
                    user_id=PAT.id,
                    wallet_id=(await wallet(session, "user", PAT.id)).id,
                    kind="pack",
                    product="pack_s_v1",
                    stars=50,
                    credits=650,
                    payload="p",
                ),
                ModelPin(role="chat", pin={"model": "old/model"}, source="seed"),
                ScoutProposal(
                    role="chat",
                    current={"model": "old/model"},
                    proposed={"model": "new/model"},
                    reason="cheaper",
                ),
            ]
        )

    sent = await conversation.send(OWNER, text="/stats")
    reply = await conversation.expect_reply(sent)
    text = reply["rich_message"]["markdown"]
    assert "Provider cost (24 h · 7 d · 30 d): $0.32 · $0.32 · $0.32" in text
    assert "paid $0.30" in text and "creator $0.02" in text and "Revenue (24 h · 7 d · 30 d): 50⭐" in text
    assert "Yard (`-1040`) — $0.30" in text and "OpenRouter key: $12.50 left of $50.00" in text
    assert "#1 chat: `old/model` → `new/model` — cheaper" in text
    apply, _ = reply["reply_markup"]["inline_keyboard"][0]
    assert apply["callback_data"] == ConsoleAction(action="apply", key="1").pack()

    stats_id = max(mid for (cid, mid) in conversation.api.messages if cid == OWNER.id)
    await conversation.press(OWNER, OWNER.id, stats_id, apply["callback_data"])
    assert conversation.api.calls_of("answerCallbackQuery")[-1]["text"] == "Proposal #1 applied."
    async with conversation.db.session() as session:
        pin = await session.get(ModelPin, "chat")
        assert pin is not None and pin.pin["model"] == "new/model" and pin.source == "owner"
    edited = conversation.api.calls_of("editMessageText")[-1]["rich_message"]["markdown"]
    assert "Roll back" not in edited and "`new/model` (owner)" in edited
    rollback = ConsoleAction(action="rollback", key="chat").pack()
    await conversation.press(OWNER, OWNER.id, stats_id, rollback)
    async with conversation.db.session() as session:
        pin = await session.get(ModelPin, "chat")
        assert pin is not None and pin.pin["model"] == "old/model"


async def test_a_scout_ping_carries_its_proposal_buttons(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        session.add(ModelPin(role="chat", pin={"model": "old/model"}, source="seed"))
        proposal = ScoutProposal(
            role="chat", current={"model": "old/model"}, proposed={"model": "new/model"}, reason="cheaper"
        )
        session.add(proposal)
        await session.flush()
        proposal_id = proposal.id
    assert conversation.services is not None
    services = conversation.services
    after = conversation.seq
    await notify_owners(
        services.bot, services.settings, f"Model scout:\nchat: proposal #{proposal_id} — cheaper",
        reply_markup=proposal_buttons([proposal_id]),
    )  # fmt: skip
    ping = conversation.api.calls_of("sendMessage", after=after)[-1]
    apply, ignore = ping["reply_markup"]["inline_keyboard"][0]
    assert (apply["text"], ignore["text"]) == (f"Apply #{proposal_id}", f"Ignore #{proposal_id}")
    ping_id = max(mid for (cid, mid) in conversation.api.messages if cid == OWNER.id)
    await conversation.press(OWNER, OWNER.id, ping_id, apply["callback_data"])
    async with conversation.db.session() as session:
        pin = await session.get(ModelPin, "chat")
        assert pin is not None and pin.pin["model"] == "new/model"
    edited = conversation.api.calls_of("editMessageReplyMarkup", after=after)[-1]
    assert "reply_markup" not in edited.fields or not edited["reply_markup"]  # the ping keeps its text
    assert not conversation.api.calls_of("editMessageText", after=after)


async def test_owners_see_the_console_in_help(conversation: Conversation) -> None:
    owner_help = await conversation.send(OWNER, text="/help")
    text = (await conversation.expect_reply(owner_help))["rich_message"]["markdown"]
    assert "**Owner**" in text and "/stats" in text and "/feedbacks" in text and "/refund" in text
    stranger_help = await conversation.send(PAT, text="/help")
    assert "/stats" not in (await conversation.expect_reply(stranger_help))["rich_message"]["markdown"]


async def test_strangers_dont_find_the_console(conversation: Conversation) -> None:
    sent = await conversation.send(PAT, text="/stats")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"].startswith("There's no /stats.")


async def test_grant_credits_a_wallet_and_tells_the_person(conversation: Conversation) -> None:
    await conversation.settle(await conversation.send(PAT, text="/start"))
    sent = await conversation.send(OWNER, text=f"/grant {PAT.id} 300")
    reply = await conversation.expect_reply(sent)
    assert (
        reply["rich_message"]["markdown"]
        == f"Granted 300 dc to user `{PAT.id}`; balance 300. They were told."
    )
    told = [
        c for c in conversation.api.calls_of("sendRichMessage", sent.after) if int(c["chat_id"]) == PAT.id
    ]
    assert "**+300** dc" in told[0]["rich_message"]["markdown"]
    async with conversation.db.session() as session:
        assert await balance(session, (await wallet(session, "user", PAT.id)).id) == 300


async def test_blocked_people_and_chats_get_no_answers(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await conversation.settle(await conversation.send(PAT, text="/start"))
    await conversation.expect_reply(await conversation.send(OWNER, text=f"/block {PAT.id}"))
    asked = await conversation.send(PAT, text="tell me a joke")
    await conversation.settle(asked)
    assert not model.requests and not conversation.api.calls_of("sendRichMessage", asked.after)
    balance_check = await conversation.send(PAT, text="/credits")  # commands keep working
    await conversation.expect_reply(balance_check)

    await conversation.expect_reply(await conversation.send(OWNER, text=f"/unblock {PAT.id}"))
    conversation.meet(PAT, YARD)
    await conversation.settle(await conversation.send(PAT, YARD, "hi all"))
    await conversation.expect_reply(await conversation.send(OWNER, text=f"/block {YARD.id}"))
    async with conversation.db.session() as session:
        assert (await session.scalar(select(Chat.blocked_at).where(Chat.id == YARD.id))) is not None
    asked = await conversation.send(PAT, YARD, "derp tell me a joke")
    await conversation.settle(asked)
    assert not model.requests


async def test_blocking_pauses_jobs_and_stops_storing(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(tool_call("schedule", text="stretch", in_minutes=30), "Will do.")
    asked = await conversation.send(PAT, text="remind me in 30 min to stretch")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    await conversation.expect_reply(await conversation.send(OWNER, text=f"/block {PAT.id}"))
    ignored = await conversation.send(PAT, text="are you there?")
    await conversation.settle(ignored)
    async with conversation.db.transaction() as session:
        await session.execute(update(Job).values(next_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert conversation.services is not None and conversation.dispatcher is not None
    after = conversation.seq
    await run_due(conversation.db, conversation.services.bot, conversation.dispatcher["runtime"])
    assert not conversation.api.calls_of("sendRichMessage", after)
    async with conversation.db.session() as session:
        assert await session.scalar(select(Job.state)) == "paused"
        assert await session.get(Message, (PAT.id, ignored.message_id)) is None
