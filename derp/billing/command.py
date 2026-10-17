"""/buy, invoices, pre-checkout and successful payments.

Private chats buy for the user and also offer Derp+ (a personal 30-day
subscription, one at a time); groups ask first whether the Derpcoins are for
the buyer or for the chat. Pressing a pack posts a Stars invoice; anyone may
pay a chat invoice, but a personal one only by the person it was made for.
"""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BotSubscriptionUpdated,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import select

from derp.billing.models import Subscription
from derp.billing.purchases import (
    Order,
    current_plan,
    find_pack,
    parse_payload,
    plan_order,
    record_payment,
    refusal,
)
from derp.billing.refunds import apply_refund, refund
from derp.config import Pack, Settings
from derp.db.engine import Database
from derp.locales import resolve, t
from derp.money.wallets import balance, wallet
from derp.ops.owner import notify_owners
from derp.telegram.sender import Target, send_markdown, send_privately


class BuyAction(CallbackData, prefix="buy"):
    target: str  # user | chat
    product: str = ""  # pack or plan id; empty: show the packs for this target


def packs_keyboard(
    settings: Settings, target: str, locale: str, *, plan: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{pack.stars}⭐ → {pack.credits:,}",
                callback_data=BuyAction(target=target, product=pack.id).pack(),
            )
        ]
        for pack in settings.packs
    ]
    if plan:
        offer = settings.plan
        button = InlineKeyboardButton(
            text=t(locale, "billing.plan_button", credits=f"{offer.credits:,}", stars=offer.stars),
            callback_data=BuyAction(target="user", product=offer.id).pack(),
            style="primary",
        )
        rows.insert(0, [button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_pack_invoice(
    bot: Bot, chat_id: int, thread_id: int | None, locale: str, pack: Pack, *, kind: str, owner_id: int
) -> Message:
    """Post the Stars invoice for one pack (never ephemeral: an invoice must persist to be paid)."""
    order = Order(pack.id, pack.credits, kind, owner_id)
    whom = t(locale, "billing.invoice_for_me") if kind == "user" else t(locale, "billing.invoice_for_chat")
    title = t(locale, "billing.invoice_title", credits=f"{pack.credits:,}")
    return await bot.send_invoice(
        chat_id=chat_id,
        message_thread_id=thread_id,
        title=title,
        description=t(locale, "billing.invoice_description", whom=whom),
        payload=order.payload,
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=pack.stars)],
    )


async def _balance(db: Database, kind: str, owner_id: int) -> int:
    async with db.transaction() as session:
        return await balance(session, (await wallet(session, kind, owner_id)).id)


async def personal_menu(
    db: Database, settings: Settings, user_id: int, locale: str
) -> tuple[str, InlineKeyboardMarkup]:
    """The private buy menu: the person's balance, Derp+ and the packs."""
    mine = await _balance(db, "user", user_id)
    return t(locale, "billing.menu", balance=f"{mine:,}"), packs_keyboard(settings, "user", locale, plan=True)


async def show_buy(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    if message.chat.type == "private":
        text, markup = await personal_menu(db, settings, message.from_user.id, locale)
        await send_markdown(bot, target, text, reply_markup=markup)
        return
    chooser = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(locale, "billing.for_me"), callback_data=BuyAction(target="user").pack()
                ),
                InlineKeyboardButton(
                    text=t(locale, "billing.for_chat"), callback_data=BuyAction(target="chat").pack()
                ),
            ]
        ]
    )
    await send_markdown(bot, target, t(locale, "billing.who"), reply_markup=chooser)


async def on_buy(
    query: CallbackQuery, callback_data: BuyAction, db: Database, settings: Settings, bot: Bot
) -> None:
    locale = resolve(query.from_user.language_code)
    message = query.message
    if not isinstance(message, Message):  # too old to edit or reply to
        await query.answer(t(locale, "billing.stale"), show_alert=True)
        return
    chat = message.chat
    owner_id = query.from_user.id if callback_data.target == "user" or chat.type == "private" else chat.id
    kind = "user" if owner_id == query.from_user.id else "chat"
    if not callback_data.product:
        left = await _balance(db, kind, owner_id)
        if kind == "user" and chat.type != "private":  # a personal balance never shows in the group
            text = t(locale, "billing.menu", balance=f"{left:,}")
            markup = packs_keyboard(settings, "user", locale, plan=True)
            delivered = await send_privately(bot, owner_id, text, reply_markup=markup)
            await query.answer(
                t(locale, "money.sent_privately" if delivered else "money.dm_first"), show_alert=True
            )
            return
        key = "billing.menu" if kind == "user" else "billing.menu_chat"
        await bot.edit_message_text(
            chat_id=chat.id,
            message_id=message.message_id,
            rich_message=InputRichMessage(markdown=t(locale, key, balance=f"{left:,}")),
            reply_markup=packs_keyboard(settings, kind, locale, plan=chat.type == "private"),
        )
        await query.answer()
        return
    if callback_data.product == settings.plan.id:
        await _offer_plan(query, message, db, settings, bot, locale)
        return
    pack = find_pack(settings, callback_data.product)
    if pack is None:
        await query.answer(t(locale, "billing.stale"), show_alert=True)
        return
    try:
        thread = message.message_thread_id if message.is_topic_message else None
        await send_pack_invoice(bot, chat.id, thread, locale, pack, kind=kind, owner_id=owner_id)
    except TelegramAPIError:
        await query.answer(t(locale, "billing.unavailable"), show_alert=True)
        return
    await query.answer()


async def _offer_plan(
    query: CallbackQuery, message: Message, db: Database, settings: Settings, bot: Bot, locale: str
) -> None:
    async with db.session() as session:
        running = await current_plan(session, query.from_user.id)
    if running is not None:
        await query.answer(
            t(locale, "billing.already_subscribed", date=f"{running.period_end:%Y-%m-%d}"), show_alert=True
        )
        return
    offer = settings.plan
    order = plan_order(settings, query.from_user.id)
    try:
        link = await bot.create_invoice_link(
            title=t(locale, "billing.plan_title"),
            description=t(locale, "billing.plan_description", credits=f"{offer.credits:,}"),
            payload=order.payload,
            currency="XTR",
            prices=[LabeledPrice(label=t(locale, "billing.plan_title"), amount=offer.stars)],
            subscription_period=offer.period_seconds,
        )
    except TelegramAPIError:
        await query.answer(t(locale, "billing.unavailable"), show_alert=True)
        return
    pay = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(locale, "billing.plan_pay", stars=offer.stars), url=link)]
        ]
    )
    text = t(locale, "billing.plan_offer", credits=f"{offer.credits:,}", stars=offer.stars)
    await send_markdown(bot, Target(message.chat.id), text, reply_markup=pay)
    await query.answer()


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


async def on_payment(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
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
    if order.kind == "plan":
        key = (
            "billing.plan_renewed"
            if payment.is_recurring and not payment.is_first_recurring
            else "billing.plan_thanks"
        )
        until = f"{purchase.expires_at:%Y-%m-%d}" if purchase.expires_at else ""
        text = t(locale, key, credits=f"{order.credits:,}", balance=f"{left:,}", date=until)
    else:
        key = "billing.thanks" if order.target == "user" else "billing.thanks_chat"
        text = t(locale, key, credits=f"{order.credits:,}", balance=f"{left:,}")
    await send_markdown(bot, Target.reply_to_message(message), text)
    await notify_owners(
        bot,
        settings,
        f"⭐ {purchase.stars} · {purchase.product} · {order.target} wallet · +{order.credits} dc",
    )


async def on_subscription(update: BotSubscriptionUpdated, db: Database, settings: Settings, bot: Bot) -> None:
    """The user canceled, re-enabled, or failed to pay for Derp+ (a canceled one runs to its period end)."""
    if update.state not in {"active", "canceled", "failed"}:
        return
    async with db.transaction() as session:
        found = await session.scalar(
            select(Subscription).where(Subscription.payload == update.invoice_payload)
        )
        if found is None or found.user_id != update.user.id:
            return
        found.state = update.state
        plan = found.plan
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
    result = await refund(db, bot, charge)
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
    router.callback_query.register(on_buy, BuyAction.filter())
    router.pre_checkout_query.register(on_pre_checkout)
    router.subscription.register(on_subscription)
    return router
