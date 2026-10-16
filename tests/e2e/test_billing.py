from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from derp.billing.models import Purchase, Subscription
from derp.money.models import CreditLot
from derp.money.wallets import balance, wallet
from tests.e2e.harness import Conversation, Person, Room

CARL = Person(1101, "Carl")
DANA = Person(1102, "Dana", language_code="ru")
OWNER = Person(1000, "Owner")
PUB = Room(-1011, title="Pub")


async def press(conversation: Conversation, person: Person, chat_id: int, message_id: int, data: str) -> None:
    query_id = f"q{len(conversation.api.calls)}"
    message = conversation.api.expect_callback(query_id, chat_id=chat_id, message_id=message_id, data=data)
    update_id = await conversation.push(
        "callback_query",
        {"id": query_id, "from": person.payload(), "chat_instance": "c", "message": message, "data": data},
    )
    await conversation.settle(update_id)


async def checkout(conversation: Conversation, person: Person, payload: str, stars: int) -> dict[str, Any]:
    after = conversation.seq
    update_id = await conversation.push(
        "pre_checkout_query",
        {
            "id": f"pc{after}",
            "from": person.payload(),
            "currency": "XTR",
            "total_amount": stars,
            "invoice_payload": payload,
        },
    )
    await conversation.settle(update_id)
    return dict(conversation.api.calls_of("answerPreCheckoutQuery", after=after)[-1].fields)


def paid(payload: str, stars: int, charge: str) -> dict[str, Any]:
    return {
        "currency": "XTR",
        "total_amount": stars,
        "invoice_payload": payload,
        "telegram_payment_charge_id": charge,
        "provider_payment_charge_id": "",
    }


async def wallet_balance(conversation: Conversation, kind: str, owner_id: int) -> int:
    async with conversation.db.transaction() as session:
        return await balance(session, (await wallet(session, kind, owner_id)).id)


async def test_private_purchase_credits_once_and_pings_the_owner(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    asked = await conversation.send(CARL, text="/buy")
    menu = await conversation.expect_reply(asked)
    assert "balance: **0**" in menu["rich_message"]["markdown"]
    buttons = [row[0] for row in menu["reply_markup"]["inline_keyboard"]][1:]  # Derp+ comes first
    assert [b["text"] for b in buttons] == ["50⭐ → 650", "250⭐ → 3,250", "750⭐ → 9,750"]
    menu_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    after = conversation.seq
    await press(conversation, CARL, CARL.id, menu_id, buttons[0]["callback_data"])
    invoice = conversation.api.calls_of("sendInvoice", after=after)[-1]
    payload = str(invoice["payload"])
    assert payload == f"p1:pack_s_v1:650:user:{CARL.id}" and "/terms" in str(invoice["description"])

    assert (await checkout(conversation, CARL, payload, 50))["ok"] in (True, "true")
    for charge in ("ch-1", "ch-1"):  # Telegram may deliver a payment twice
        sent = await conversation.send(CARL, successful_payment=paid(payload, 50, charge))
        await conversation.settle(sent)
    assert await wallet_balance(conversation, "user", CARL.id) == 650
    async with conversation.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(Purchase)) == 1
    thanks = [c for c in conversation.api.calls_of("sendRichMessage") if int(c["chat_id"]) == CARL.id]
    assert sum("+650" in c["rich_message"]["markdown"] for c in thanks) == 1
    pings = [c for c in conversation.api.calls_of("sendMessage") if int(c["chat_id"]) == OWNER.id]
    assert len(pings) == 1 and "pack_s_v1" in str(pings[0]["text"])


async def test_group_purchase_for_the_chat_can_be_paid_by_anyone(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    conversation.meet(CARL, PUB)
    conversation.meet(DANA, PUB)
    asked = await conversation.send(CARL, PUB, "/buy")
    chooser = await conversation.expect_reply(asked)
    for_chat = chooser["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    menu_id = max(mid for (cid, mid) in conversation.api.messages if cid == PUB.id)
    await press(conversation, CARL, PUB.id, menu_id, for_chat)
    edited = conversation.api.calls_of("editMessageText")[-1]
    assert "Chat balance" in edited["rich_message"]["markdown"]
    pack = edited["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
    after = conversation.seq
    await press(conversation, CARL, PUB.id, menu_id, pack)
    payload = str(conversation.api.calls_of("sendInvoice", after=after)[-1]["payload"])
    assert payload == f"p1:pack_m_v1:3250:chat:{PUB.id}"

    assert (await checkout(conversation, DANA, payload, 250))["ok"] in (True, "true")
    sent = await conversation.send(DANA, PUB, successful_payment=paid(payload, 250, "ch-2"))
    await conversation.settle(sent)
    assert await wallet_balance(conversation, "chat", PUB.id) == 3250
    assert await wallet_balance(conversation, "user", DANA.id) == 0


async def test_checkout_refuses_someone_elses_or_stale_invoices(conversation: Conversation) -> None:
    mine = f"p1:pack_s_v1:650:user:{CARL.id}"
    refused = await checkout(conversation, DANA, mine, 50)
    assert refused["ok"] in (False, "false") and "другого человека" in str(refused["error_message"])
    stale = await checkout(conversation, CARL, "p1:pack_s_v0:600:user:1101", 50)
    assert stale["ok"] in (False, "false") and "changed" in str(stale["error_message"])
    cheaper = await checkout(conversation, CARL, mine, 40)
    assert cheaper["ok"] in (False, "false")


async def test_derp_plus_subscription_credits_each_cycle_once(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    asked = await conversation.send(CARL, text="/buy")
    menu = await conversation.expect_reply(asked)
    plan_button = menu["reply_markup"]["inline_keyboard"][0][0]
    assert plan_button["text"].startswith("Derp+")
    menu_id = max(mid for (cid, mid) in conversation.api.messages if cid == CARL.id)
    after = conversation.seq
    await press(conversation, CARL, CARL.id, menu_id, plan_button["callback_data"])
    link = conversation.api.calls_of("createInvoiceLink", after=after)[-1]
    payload = str(link["payload"])
    assert int(link["subscription_period"]) == 2592000 and payload.startswith(
        f"s1:derp_plus_v1:7150:{CARL.id}:"
    )
    offer = conversation.api.calls_of("sendRichMessage", after=after)[-1]
    assert offer["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://t.me/$invoice")

    assert (await checkout(conversation, CARL, payload, 500))["ok"] in (True, "true")
    now = int(datetime.now(UTC).timestamp())
    first = paid(payload, 500, "sub-1") | {
        "is_recurring": True,
        "is_first_recurring": True,
        "subscription_expiration_date": now + 30 * 86400,
    }
    sent = await conversation.send(CARL, successful_payment=first)
    await conversation.settle(sent)
    assert await wallet_balance(conversation, "user", CARL.id) == 7150

    # One Derp+ at a time: a second offer is refused before any invoice exists.
    second = f"s1:derp_plus_v1:7150:{CARL.id}:other"
    assert (await checkout(conversation, CARL, second, 500))["ok"] in (False, "false")

    renewal = paid(payload, 500, "sub-2") | {
        "is_recurring": True,
        "subscription_expiration_date": now + 60 * 86400,
    }
    sent = await conversation.send(CARL, successful_payment=renewal)
    await conversation.settle(sent)
    replies = [
        c["rich_message"]["markdown"]
        for c in conversation.api.calls_of("sendRichMessage")
        if int(c["chat_id"]) == CARL.id
    ]
    assert any("Welcome to Derp+" in r for r in replies) and any("renewed" in r for r in replies)
    async with conversation.db.session() as session:
        lots = list(
            await session.scalars(select(CreditLot).where(CreditLot.source == "plan").order_by(CreditLot.id))
        )
        assert [(lot.amount, int(lot.expires_at.timestamp())) for lot in lots if lot.expires_at] == [
            (7150, now + 30 * 86400),
            (7150, now + 60 * 86400),
        ]
        subscription = await session.scalar(select(Subscription))
        assert subscription is not None and (subscription.first_charge_id, subscription.last_charge_id) == (
            "sub-1",
            "sub-2",
        )

    update_id = await conversation.push(
        "subscription", {"user": CARL.payload(), "invoice_payload": payload, "state": "canceled"}
    )
    await conversation.settle(update_id)
    async with conversation.db.session() as session:
        assert await session.scalar(select(Subscription.state)) == "canceled"
