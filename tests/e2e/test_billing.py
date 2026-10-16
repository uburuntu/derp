from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from derp.billing.models import Purchase, Subscription
from derp.billing.refunds import reconcile_refunds
from derp.money.models import CreditLot
from derp.money.wallets import balance, capture, reserve, wallet
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


async def buy_pack(conversation: Conversation, person: Person, charge: str) -> str:
    payload = f"p1:pack_s_v1:650:user:{person.id}"
    await checkout(conversation, person, payload, 50)
    sent = await conversation.send(person, successful_payment=paid(payload, 50, charge))
    await conversation.settle(sent)
    return payload


async def test_owner_refund_takes_back_only_what_is_unspent(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    await buy_pack(conversation, CARL, "ch-r1")
    async with conversation.db.transaction() as session:
        carl = (await wallet(session, "user", CARL.id)).id
        await reserve(session, carl, "turn:spent", 150)
        await capture(session, "turn:spent", 150)

    stranger = await conversation.send(DANA, text="/refund ch-r1")
    await conversation.settle(stranger)
    assert conversation.api.calls_of("refundStarPayment") == []  # owners only

    asked = await conversation.send(OWNER, text="/refund ch-r1")
    reply = await conversation.expect_reply(asked)
    assert "took back 500 of 650" in reply["rich_message"]["markdown"]
    call = conversation.api.calls_of("refundStarPayment")[-1]
    assert (int(call["user_id"]), call["telegram_payment_charge_id"]) == (CARL.id, "ch-r1")
    assert await wallet_balance(conversation, "user", CARL.id) == 0

    # Telegram's own refund notice and a repeated command change nothing more.
    notice = await conversation.send(
        CARL,
        refunded_payment={
            "currency": "XTR",
            "total_amount": 50,
            "invoice_payload": "x",
            "telegram_payment_charge_id": "ch-r1",
        },
    )
    await conversation.settle(notice)
    again = await conversation.send(OWNER, text="/refund ch-r1")
    assert "Already refunded" in (await conversation.expect_reply(again))["rich_message"]["markdown"]
    assert len(conversation.api.calls_of("refundStarPayment")) == 1
    async with conversation.db.session() as session:
        assert await session.scalar(select(Purchase.status)) == "refunded"


async def test_refunding_derp_plus_stops_renewal(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    payload = f"s1:derp_plus_v1:7150:{CARL.id}:abc"
    await checkout(conversation, CARL, payload, 500)
    expires = int(datetime.now(UTC).timestamp()) + 30 * 86400
    first = paid(payload, 500, "sub-r1") | {
        "is_recurring": True,
        "is_first_recurring": True,
        "subscription_expiration_date": expires,
    }
    sent = await conversation.send(CARL, successful_payment=first)
    await conversation.settle(sent)
    asked = await conversation.send(OWNER, text="/refund sub-r1")
    await conversation.expect_reply(asked)
    cancel = conversation.api.calls_of("editUserStarSubscription")[-1]
    assert cancel["telegram_payment_charge_id"] == "sub-r1" and cancel["is_canceled"] in (True, "true")
    async with conversation.db.session() as session:
        assert await session.scalar(select(Subscription.state)) == "canceled"
    assert await wallet_balance(conversation, "user", CARL.id) == 0


async def test_daily_sweep_applies_chargebacks_nobody_announced(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    await buy_pack(conversation, CARL, "ch-s1")
    await buy_pack(conversation, DANA, "ch-s2")
    conversation.api.star_transactions = [
        {
            "id": "ch-s1",
            "amount": 50,
            "date": 0,
            "source": {"type": "user", "transaction_type": "invoice_payment", "user": CARL.payload()},
        },
        {
            "id": "ch-s2",
            "amount": 50,
            "date": 0,
            "source": {"type": "user", "transaction_type": "invoice_payment", "user": DANA.payload()},
        },
        {
            "id": "ch-s1",
            "amount": 50,
            "date": 1,
            "receiver": {"type": "user", "transaction_type": "invoice_payment", "user": CARL.payload()},
        },
    ]
    assert conversation.services is not None
    bot = conversation.services.bot
    assert await reconcile_refunds(conversation.db, bot) == 1
    assert await reconcile_refunds(conversation.db, bot) == 0
    assert await wallet_balance(conversation, "user", CARL.id) == 0
    assert await wallet_balance(conversation, "user", DANA.id) == 650
