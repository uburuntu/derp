"""/buy, invoices, pre-checkout and successful payments.

Private chats buy for the user; groups ask first whether the Derpcoins are
for the buyer or for the chat. Pressing a pack posts a Stars invoice; anyone
may pay a chat invoice, but a personal one only by the person it was made for.
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from derp.billing.purchases import Order, find_pack, parse_payload, record_payment, refusal
from derp.config import Settings
from derp.db.engine import Database
from derp.locales import resolve, t
from derp.money.wallets import balance, wallet
from derp.ops.owner import notify_owners
from derp.telegram.sender import Target, send_markdown


class BuyAction(CallbackData, prefix="buy"):
    target: str  # user | chat
    product: str = ""  # pack id; empty: show the packs for this target


def _packs_keyboard(settings: Settings, target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{pack.stars}⭐ → {pack.credits:,}",
                    callback_data=BuyAction(target=target, product=pack.id).pack(),
                )
            ]
            for pack in settings.packs
        ]
    )


async def _balance(db: Database, kind: str, owner_id: int) -> int:
    async with db.transaction() as session:
        return await balance(session, (await wallet(session, kind, owner_id)).id)


async def show_buy(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    if message.chat.type == "private":
        mine = await _balance(db, "user", message.from_user.id)
        text = t(locale, "billing.menu", balance=f"{mine:,}")
        await send_markdown(bot, target, text, reply_markup=_packs_keyboard(settings, "user"))
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
        key = "billing.menu" if kind == "user" else "billing.menu_chat"
        await bot.edit_message_text(
            chat_id=chat.id,
            message_id=message.message_id,
            rich_message=InputRichMessage(markdown=t(locale, key, balance=f"{left:,}")),
            reply_markup=_packs_keyboard(settings, kind),
        )
        await query.answer()
        return
    pack = find_pack(settings, callback_data.product)
    if pack is None:
        await query.answer(t(locale, "billing.stale"), show_alert=True)
        return
    order = Order(pack.id, pack.credits, kind, owner_id)
    whom = t(locale, "billing.invoice_for_me") if kind == "user" else t(locale, "billing.invoice_for_chat")
    await bot.send_invoice(
        chat_id=chat.id,
        message_thread_id=message.message_thread_id if message.is_topic_message else None,
        title=t(locale, "billing.invoice_title", credits=f"{pack.credits:,}"),
        description=t(locale, "billing.invoice_description", whom=whom),
        payload=order.payload,
        currency="XTR",
        prices=[
            LabeledPrice(
                label=t(locale, "billing.invoice_title", credits=f"{pack.credits:,}"), amount=pack.stars
            )
        ],
    )
    await query.answer()


async def on_pre_checkout(query: PreCheckoutQuery, settings: Settings) -> None:
    locale = resolve(query.from_user.language_code)
    reason = refusal(
        settings,
        parse_payload(query.invoice_payload),
        currency=query.currency,
        stars=query.total_amount,
        payer_id=query.from_user.id,
    )
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
    async with db.transaction() as session:
        purchase, new = await record_payment(session, order, payment, message.from_user.id)
        left = await balance(session, purchase.wallet_id)
    if not new:
        return
    key = "billing.thanks" if order.target == "user" else "billing.thanks_chat"
    await send_markdown(
        bot,
        Target.reply_to_message(message),
        t(locale, key, credits=f"{order.credits:,}", balance=f"{left:,}"),
    )
    await notify_owners(
        bot,
        settings,
        f"⭐ {purchase.stars} · {purchase.product} · {order.target} wallet · +{order.credits} dc",
    )


def build_router() -> Router:
    router = Router(name="billing")
    router.message.register(show_buy, Command("buy"))
    router.message.register(on_payment, F.successful_payment)
    router.callback_query.register(on_buy, BuyAction.filter())
    router.pre_checkout_query.register(on_pre_checkout)
    return router
