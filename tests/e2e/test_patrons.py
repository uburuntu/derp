"""Buying Derpcoins for a chat: the patron post, anonymity, coalescing and who chipped in lately."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from derp.billing.models import PatronPost, Purchase
from derp.billing.patrons import PatronAction, expire_hide_buttons
from derp.billing.refunds import apply_refund
from derp.history.models import User
from derp.money.wallets import wallet
from tests.e2e.fake_api import Call
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_billing import checkout, ephemeral_menu, link_payloads, paid

ALICE = Person(3101, "Alice")
BOB = Person(3102, "Bob")
CARL = Person(3103, "Carl")
ERIN = Person(3104, "Erin")
OWNER = Person(1000, "Owner")
PUB = Room(-3011, title="Pub")
PACKS = {650: ("pack_s_v1", 50), 3250: ("pack_m_v1", 250)}


async def chip_in(conversation: Conversation, person: Person, credits: int, charge: str) -> int:
    """``person`` pays a chat invoice posted in the pub; returns the call sequence before the payment."""
    product, stars = PACKS[credits]
    payload = f"p1:{product}:{credits}:chat:{PUB.id}"
    await checkout(conversation, person, payload, stars)
    sent = await conversation.send(person, PUB, successful_payment=paid(payload, stars, charge))
    await conversation.settle(sent)
    return sent.after


def posts(conversation: Conversation, after: int) -> list[Call]:
    return [
        c for c in conversation.api.calls_of("sendRichMessage", after=after) if int(c["chat_id"]) == PUB.id
    ]


def buttons(call: Call | dict[str, Any]) -> list[str]:
    markup = call["reply_markup"] if isinstance(call, dict) else call.fields.get("reply_markup") or {}
    return [b["text"] for row in markup.get("inline_keyboard", []) for b in row]


async def test_the_patron_post_names_coalesces_and_lets_the_buyer_hide(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    for person in (ALICE, BOB, CARL):
        conversation.meet(person, PUB)

    after = await chip_in(conversation, ALICE, 650, "ch-a1")
    [post] = posts(conversation, after)
    text = post["rich_message"]["markdown"]
    assert "**Alice**" in text and "**650** Derpcoins" in text and "Wallet: **650** dc" in text
    assert post["disable_notification"] in (True, "true") and buttons(post) == [
        "⭐ Chip in too",
        "🙈 Hide my name",
    ]
    assert not conversation.api.calls_of("pinChatMessage")
    post_id = max(mid for (cid, mid) in conversation.api.messages if cid == PUB.id)

    after = await chip_in(conversation, BOB, 3250, "ch-b1")
    assert posts(conversation, after) == []  # within half an hour the post is edited instead
    [edit] = conversation.api.calls_of("editMessageText", after=after)
    assert int(edit["message_id"]) == post_id
    assert (
        "**Alice** and **Bob**" in edit["rich_message"]["markdown"]
        and "**3,900**" in edit["rich_message"]["markdown"]
    )

    async with conversation.db.session() as session:
        row = await session.scalar(select(PatronPost))
        assert row is not None
    hide = PatronAction(post=row.id).pack()
    after = conversation.seq
    await conversation.press(CARL, PUB.id, post_id, hide)
    assert "Only the person who chipped in" in str(
        conversation.api.calls_of("answerCallbackQuery", after=after)[-1]["text"]
    )
    assert not conversation.api.calls_of("editMessageText", after=after)

    after = conversation.seq
    await conversation.press(BOB, PUB.id, post_id, hide)
    [edit] = conversation.api.calls_of("editMessageText", after=after)
    assert "**Alice** and a mysterious patron" in edit["rich_message"]["markdown"]
    assert "Bob" not in edit["rich_message"]["markdown"]
    assert buttons(conversation.api.message(PUB.id, post_id)) == [
        "⭐ Chip in too",
        "🙈 Hide my name",
    ]  # Alice still may
    assert "mysterious patron now" in str(
        conversation.api.calls_of("answerCallbackQuery", after=after)[-1]["text"]
    )
    async with conversation.db.session() as session:
        assert (await session.get(User, BOB.id)).patron_named is False  # type: ignore[union-attr]

    after = conversation.seq
    await conversation.press(BOB, PUB.id, post_id, hide)  # already anonymous: refused, the button stays
    assert "Too late" in str(conversation.api.calls_of("answerCallbackQuery", after=after)[-1]["text"])
    assert not conversation.api.calls_of("editMessageReplyMarkup", after=after)
    assert buttons(conversation.api.message(PUB.id, post_id)) == ["⭐ Chip in too", "🙈 Hide my name"]

    after = conversation.seq
    chip = conversation.api.message(PUB.id, post_id)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await conversation.press(CARL, PUB.id, post_id, chip)
    menu = ephemeral_menu(conversation, after)
    assert int(menu["ephemeral_message_parameters"]["receiver_user_id"]) == CARL.id
    assert link_payloads(conversation, menu)[0] == f"p1:pack_s_v1:650:chat:{PUB.id}"

    # Ten minutes later the name can't be hidden any more; half an hour later a purchase posts afresh.
    earlier = datetime.now(UTC) - timedelta(minutes=31)
    async with conversation.db.transaction() as session:
        await session.execute(update(Purchase).values(created_at=earlier))
        await session.execute(
            update(PatronPost).values(created_at=earlier, hide_until=earlier + timedelta(minutes=10))
        )
    after = conversation.seq
    await conversation.press(ALICE, PUB.id, post_id, hide)
    assert "Too late" in str(conversation.api.calls_of("answerCallbackQuery", after=after)[-1]["text"])
    assert conversation.services is not None
    await expire_hide_buttons(conversation.db, conversation.services.bot)  # the sweep drops the button
    assert buttons(conversation.api.message(PUB.id, post_id)) == ["⭐ Chip in too"]

    after = await chip_in(conversation, BOB, 650, "ch-b2")
    [fresh] = posts(conversation, after)
    assert "A mysterious patron" in fresh["rich_message"]["markdown"]  # Bob stays anonymous from now on
    assert buttons(fresh) == ["⭐ Chip in too"]


async def test_a_refunded_purchase_drops_out_of_the_post(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    for person in (ALICE, BOB, CARL):
        conversation.meet(person, PUB)
    await chip_in(conversation, ALICE, 650, "ch-r1")
    await chip_in(conversation, BOB, 3250, "ch-r2")
    assert await apply_refund(conversation.db, "ch-r2") is not None

    after = await chip_in(conversation, CARL, 650, "ch-r3")
    [edit] = conversation.api.calls_of("editMessageText", after=after)
    text = edit["rich_message"]["markdown"]
    assert "**Alice** and **Carl**" in text and "**1,300** Derpcoins" in text and "Bob" not in text


async def test_asking_derp_to_stay_anonymous_hides_the_name(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(OWNER)
    conversation.meet(ERIN, PUB)
    model.reply(tool_call("change_setting", setting="name_me_when_i_chip_in", value="off"), "done")
    asked = await conversation.send(ERIN, text="don't name me when I buy coins for a chat")
    await conversation.settle(asked)
    async with conversation.db.session() as session:
        assert (await session.get(User, ERIN.id)).patron_named is False  # type: ignore[union-attr]
    after = await chip_in(conversation, ERIN, 650, "ch-e1")
    [post] = posts(conversation, after)
    assert (
        "A mysterious patron" in post["rich_message"]["markdown"]
        and "Erin" not in post["rich_message"]["markdown"]
    )
    assert buttons(post) == ["⭐ Chip in too"]


async def test_the_hide_button_goes_away_after_its_window(conversation: Conversation) -> None:
    conversation.meet(OWNER)
    conversation.meet(ALICE, PUB)
    await chip_in(conversation, ALICE, 650, "ch-w1")
    post_id = max(mid for (cid, mid) in conversation.api.messages if cid == PUB.id)
    assert conversation.services is not None
    assert await expire_hide_buttons(conversation.db, conversation.services.bot) == 0  # not yet
    async with conversation.db.transaction() as session:
        await session.execute(update(PatronPost).values(hide_until=datetime.now(UTC) - timedelta(seconds=1)))
    assert await expire_hide_buttons(conversation.db, conversation.services.bot) == 1
    assert buttons(conversation.api.message(PUB.id, post_id)) == ["⭐ Chip in too"]
    async with conversation.db.session() as session:
        assert await session.scalar(select(PatronPost.hide_until)) is None


async def test_help_and_credits_name_recent_patrons(conversation: Conversation) -> None:
    people = {ALICE: (650, True), BOB: (3250, True), CARL: (9750, False), ERIN: (1300, True)}
    conversation.meet(ALICE, PUB)
    conversation.meet(Person(3105, "Refunded"), PUB)
    async with conversation.db.transaction() as session:
        chat_wallet = (await wallet(session, "chat", PUB.id)).id
        session.add(User(id=3105, first_name="Refunded"))
        for person, (_, named) in people.items():
            session.add(User(id=person.id, first_name=person.first_name, patron_named=named))
        await session.flush()
        rows = [(person.id, amount, "paid") for person, (amount, _) in people.items()]
        rows.append((3105, 20_000, "refunded"))
        for index, (user_id, amount, status) in enumerate(rows):
            session.add(
                Purchase(
                    telegram_charge_id=f"lately-{index}", user_id=user_id, wallet_id=chat_wallet, kind="pack",
                    product="pack_s_v1", stars=50, credits=amount, payload="p1", status=status,
                )
            )  # fmt: skip
        old = Purchase(
            telegram_charge_id="lately-old", user_id=ALICE.id, wallet_id=chat_wallet, kind="pack",
            product="pack_l_v1", stars=750, credits=50_000, payload="p1",
            created_at=datetime.now(UTC) - timedelta(days=31),
        )  # fmt: skip
        session.add(old)

    for command in ("/help", "/credits"):
        after = conversation.seq
        await conversation.settle(await conversation.ephemeral_command(ALICE, PUB, command))
        text = conversation.api.calls_of("sendRichMessage", after=after)[-1]["rich_message"]["markdown"]
        assert "Chipped in lately: Bob, Erin, Alice" in text, (
            command
        )  # Carl stays anonymous; refunds don't count
