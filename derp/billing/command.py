"""/buy, pre-checkout, successful payments and subscription updates.

Private chats buy for the person (Derp+ and packs; a pack button posts a Stars invoice there). Group
/buy asks whose wallet first — the chat's, with its balance, or the presser's own — and each press
opens that pack menu with invoice links for the presser only (see ``menu``). Anyone may pay a chat
invoice, a personal one only the person it was made for. A purchase never shows a person's balance in
a group: the receipt goes to their private chat. A chat purchase gets the patron post instead.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BotSubscriptionUpdated, InlineKeyboardButton, Message, PreCheckoutQuery
from sqlalchemy import select

from derp.billing import patrons
from derp.billing.menu import (
    BuyAction,
    group_chooser,
    open_menu,
    personal_menu,
    plan_link,
    send_pack_invoice,
    wallet_balance,
)
from derp.billing.models import Subscription
from derp.billing.purchases import Order, current_plan, find_pack, parse_payload, record_payment, refusal
from derp.billing.refunds import apply_refund, refund, stop_renewal
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.money.free import offer_continue
from derp.money.funding import Funder
from derp.money.wallets import balance
from derp.ops.owner import notify_owners
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown, send_privately
from derp.telegram.text import literal

__all__ = ["BuyAction", "build_router"]
log = logging.getLogger("derp.billing")


async def show_buy(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    if message.chat.type == "private":
        text, keyboard = await personal_menu(db, settings, message.from_user.id, locale)
    else:
        text, keyboard = group_chooser(locale, await wallet_balance(db, "chat", message.chat.id))
    await send_markdown(bot, target, text, reply_markup=keyboard)


async def on_buy(press: Press, data: BuyAction, db: Database, settings: Settings) -> None:
    message, private = press.message, press.message.chat.type == "private"
    kind = "user" if data.target == "user" or private else "chat"
    owner_id = press.user_id if kind == "user" else press.chat_id
    if not data.product:
        if not private:
            return await open_menu(press, db, settings, kind=kind)
        # In place; the pressed message's other buttons stay (the offer's free models, the showcase's Add to group).
        rows = message.reply_markup.inline_keyboard if message.reply_markup else []
        others = [b for row in rows for b in row if not _buys(b)]
        return await press.edit(*await personal_menu(db, settings, press.user_id, press.locale, others))
    if data.product == settings.plan.id:
        return await _offer_plan(press, db, settings)
    pack = find_pack(settings, data.product)
    if pack is None:
        return await press.deny("billing.stale")
    thread = message.message_thread_id if message.is_topic_message else None
    try:
        await send_pack_invoice(press.bot, press.locale, pack, kind=kind, owner_id=owner_id, thread_id=thread)
    except TelegramAPIError:
        return await press.deny("billing.unavailable")
    return None


def _buys(b: InlineKeyboardButton) -> bool:
    return b.callback_data is not None and b.callback_data.partition(":")[0] == BuyAction.__prefix__


async def _offer_plan(press: Press, db: Database, settings: Settings) -> None:
    async with db.session() as session:
        running = await current_plan(session, press.user_id)
    if running is not None:
        return await press.deny("billing.already_subscribed", date=f"{running.period_end:%Y-%m-%d}")
    offer = settings.plan
    try:
        link = await plan_link(press.bot, settings, press.locale, press.user_id)
    except TelegramAPIError:
        return await press.deny("billing.unavailable")
    pay = markup(InlineKeyboardButton(text=t(press.locale, "billing.plan_pay", stars=offer.stars), url=link))
    text = t(press.locale, "billing.plan_offer", credits=f"{offer.credits:,}", stars=offer.stars)
    if not await send_privately(press.bot, press.user_id, text, reply_markup=pay):
        return await press.deny("money.dm_first")
    return None


async def on_pre_checkout(query: PreCheckoutQuery, db: Database, settings: Settings) -> None:
    locale = resolve(query.from_user.language_code)
    order = parse_payload(query.invoice_payload)
    reason = refusal(
        settings, order, currency=query.currency, stars=query.total_amount, payer_id=query.from_user.id
    )
    if reason is None and order is not None and order.kind == "plan":
        async with db.session() as session:  # one Derp+ at a time: Telegram would allow several
            running = await current_plan(session, query.from_user.id)
            if running is not None and running.payload != query.invoice_payload:  # renewals pass
                reason = "billing.already_subscribed_short"
    if reason is None:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message=t(locale, reason))


async def on_payment(message: Message, db: Database, settings: Settings, bot: Bot, funder: Funder) -> None:
    """Record a Stars payment once, then thank the payer where only they see their balance.

    Invoice links are paid in the payer's private chat; invoices posted in a group report there.
    """
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    order = parse_payload(payment.invoice_payload)
    if order is None:  # can't happen for our own invoices; keep the money trail for the owner
        await notify_owners(
            bot, settings, f"Unrecognized Stars payment {payment.telegram_payment_charge_id}."
        )
        return
    try:
        async with db.transaction() as session:
            purchase, new = await record_payment(session, order, payment, message.from_user.id)
            left = await balance(session, purchase.wallet_id)
    except Exception as error:
        await notify_owners(
            bot,
            settings,
            f"Stars payment {payment.telegram_payment_charge_id} wasn't recorded ({type(error).__name__}); "
            "the Star transaction sweep will retry it.",
        )
        raise
    if not new:
        return
    added, amount = f"{order.credits:,}", f"{left:,}"
    chat_id, thread_id = _paid_for(message, order)
    if order.target == "chat":
        await _thank_patron(message, db, bot, funder, order, purchase.id, locale, amount, thread_id)
    else:
        if order.kind == "plan":
            renewed = payment.is_recurring and not payment.is_first_recurring
            until = f"{purchase.expires_at:%Y-%m-%d}" if purchase.expires_at else ""
            key = "billing.plan_renewed" if renewed else "billing.plan_thanks"
            text = t(locale, key, credits=added, balance=amount, date=until)
        else:
            text = t(locale, "billing.thanks", credits=added, balance=amount)
        await _receipt(bot, message, locale, text, added)
    await offer_continue(  # the buyer's own ask that ran out of Derpcoins there can go on now
        db, bot, settings, chat_id=chat_id, thread_id=thread_id, user_id=message.from_user.id, locale=locale
    )
    await notify_owners(
        bot,
        settings,
        f"⭐ {purchase.stars} · {purchase.product} · {order.target} wallet · +{order.credits} dc",
    )


def _paid_for(message: Message, order: Order) -> tuple[int, int | None]:
    """The chat and forum topic a purchase is for: a chat pack's chat (its topic from the invoice, or where
    it was paid), else the chat the payment came in."""
    here = message.message_thread_id if message.is_topic_message else None
    if order.target != "chat":
        return message.chat.id, here
    return order.target_id, order.thread_id or (here if message.chat.id == order.target_id else None)


async def _receipt(bot: Bot, message: Message, locale: str, text: str, added: str) -> None:
    """The payer's receipt with their balance: in their private chat, never in a group."""
    if message.chat.type == "private":
        await send_markdown(bot, Target.reply_to_message(message), text)
        return
    await send_markdown(
        bot, Target.reply_to_message(message), t(locale, "billing.thanks_group", credits=added)
    )
    if message.from_user is not None:
        await send_privately(bot, message.from_user.id, text)


async def _thank_patron(
    message: Message,
    db: Database,
    bot: Bot,
    funder: Funder,
    order: Order,
    purchase_id: int,
    locale: str,
    amount: str,
    thread: int | None,
) -> None:
    """The chat's patron post, in the topic the purchase was made in; a link payer also gets a receipt."""
    paid_here = message.chat.id == order.target_id
    try:
        await patrons.thank(
            db, bot, funder, purchase_id=purchase_id, chat_id=order.target_id, thread_id=thread, locale=locale
        )
    except Exception as error:  # noqa: BLE001 - the purchase is recorded; the post is a courtesy
        log.warning("patron post failed: %s", type(error).__name__)
    if paid_here:
        return
    async with db.session() as session:
        chat = await session.get(Chat, order.target_id)
    title = literal(one_line(chat.title)) if chat and chat.title else t(locale, "settings.this_chat")
    text = t(locale, "billing.thanks_chat", credits=f"{order.credits:,}", balance=amount, title=title)
    await send_markdown(bot, Target.reply_to_message(message), text)


async def on_subscription(update: BotSubscriptionUpdated, db: Database, settings: Settings, bot: Bot) -> None:
    """The user canceled, re-enabled, or failed to pay for Derp+ (a canceled one runs to its period end).

    A failed renewal is told to the subscriber once (until the state changes again), with a way back.
    """
    if update.state not in {"active", "canceled", "failed"}:
        return
    async with db.transaction() as session:
        found = await session.scalar(
            select(Subscription).where(Subscription.payload == update.invoice_payload)
        )
        if found is None or found.user_id != update.user.id:
            return
        newly_failed = update.state == "failed" and found.state != "failed"
        found.state = update.state
        plan, period_end = found.plan, found.period_end
    if newly_failed:
        locale = resolve(update.user.language_code)
        again = button(
            t(locale, "billing.subscribe_again"), BuyAction(target="user", product=settings.plan.id)
        )
        text = t(locale, "billing.renewal_failed", date=f"{period_end:%Y-%m-%d}")
        await send_privately(bot, update.user.id, text, reply_markup=markup(again))
    await notify_owners(bot, settings, f"Derp+ {update.state}: {plan}")


async def on_refund_command(
    message: Message, command: CommandObject, db: Database, settings: Settings, bot: Bot
) -> None:
    """Owner only (unlisted): ``/refund <telegram charge id>``. Anyone else gets silence."""
    if message.from_user is None or message.from_user.id not in settings.owner_ids:
        return
    charge = (command.args or "").strip()
    if not charge:
        await send_markdown(bot, Target.reply_to_message(message), "Usage: `/refund <charge id>`")
        return
    result = await refund(db, bot, settings, charge)
    if result is None:
        text = "No purchase with that charge id."
    elif not result.first:
        text = "Already refunded; nothing more to take back."
    else:
        p = result.purchase
        text = f"Refunded {p.stars}⭐ ({p.product}); took back {result.clawed} of {p.credits} Derpcoins."
    await send_markdown(bot, Target.reply_to_message(message), text)


async def on_refunded(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    """Telegram reports a refund (e.g. one made outside /refund): apply it once."""
    if message.refunded_payment is None:
        return
    result = await apply_refund(db, message.refunded_payment.telegram_payment_charge_id)
    if result is not None and result.first:
        await stop_renewal(db, bot, settings, result.purchase)
        p = result.purchase
        await notify_owners(
            bot, settings, f"Refund reported: {p.stars}⭐ {p.product}, took back {result.clawed} dc"
        )


def build_router() -> Router:
    router = Router(name="billing")
    router.message.register(show_buy, Command("buy"))
    router.message.register(on_payment, F.successful_payment)
    router.message.register(on_refunded, F.refunded_payment)
    router.message.register(on_refund_command, Command("refund"))
    on_press(router, BuyAction)(on_buy)
    router.pre_checkout_query.register(on_pre_checkout)
    router.subscription.register(on_subscription)
    router.include_router(patrons.build_router())
    return router
