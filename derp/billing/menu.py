"""Buy menus: what /buy, the out-of-Derpcoins offer, the patron post and Derp itself open.

Private chats get the personal menu: Derp+ and pack buttons that post a Stars invoice there. In
groups a press opens the pack menu as an ephemeral message only the presser sees, with invoice links
(URL buttons), so no invoices pile up in the chat. A chat pack's payload names the chat and topic:
link payments arrive in the payer's private chat. Telegram accepts the ephemeral message only within
15 seconds of the press and may refuse it; then the press falls back to a visible menu (chat packs)
or the private chat (personal ones).
"""

import asyncio
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message

from derp.billing.purchases import Order, current_plan, pack_order, plan_order
from derp.config import Pack, Settings
from derp.db.engine import Database
from derp.locales import t
from derp.money.wallets import balance, wallet
from derp.telegram.buttons import Press, Row, button, link, markup
from derp.telegram.sender import Target, send_markdown, send_privately


class BuyAction(CallbackData, prefix="buy"):
    target: str  # user | chat
    product: str = ""  # pack or plan id; empty: show the packs for this target


def pack_label(pack: Pack) -> str:
    return f"{pack.stars}⭐ → {pack.credits:,} dc"


def packs_keyboard(
    settings: Settings, target: str, locale: str, *rows: Row, plan: bool = False
) -> InlineKeyboardMarkup | None:
    """Pack buttons that post a Stars invoice where they are pressed (private chats), then ``rows``."""
    offer = settings.plan
    plan_button = button(
        t(locale, "billing.plan_button", credits=f"{offer.credits:,}", stars=offer.stars),
        BuyAction(target="user", product=offer.id),
        style="primary",
    )
    packs = [button(pack_label(pack), BuyAction(target=target, product=pack.id)) for pack in settings.packs]
    return markup(plan_button if plan else None, *packs, *rows)


def group_chooser(locale: str, chat_balance: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Group /buy: the chat wallet first (its balance is the chat's to see), then the presser's own."""
    keyboard = markup(
        [
            button(t(locale, "billing.for_chat"), BuyAction(target="chat"), style="primary"),
            button(t(locale, "billing.for_me"), BuyAction(target="user")),
        ]
    )
    return t(locale, "billing.who", balance=f"{chat_balance:,}"), keyboard


def _invoice(title: str, description: str, order: Order, stars: int) -> dict[str, Any]:
    """What a Stars invoice carries, posted or as a link."""
    return {
        "title": title,
        "description": description,
        "payload": order.payload,
        "currency": "XTR",
        "prices": [LabeledPrice(label=title, amount=stars)],
    }


def _pack_invoice(locale: str, pack: Pack, kind: str, owner_id: int, thread_id: int | None) -> dict[str, Any]:
    whom = t(locale, "billing.invoice_for_me" if kind == "user" else "billing.invoice_for_chat")
    title = t(locale, "billing.invoice_title", credits=f"{pack.credits:,}")
    order = pack_order(pack, kind, owner_id, thread_id)
    return _invoice(title, t(locale, "billing.invoice_description", whom=whom), order, pack.stars)


async def send_pack_invoice(
    bot: Bot, locale: str, pack: Pack, *, kind: str, owner_id: int, thread_id: int | None = None
) -> Message:
    """Post the Stars invoice for one pack where its Derpcoins go (never ephemeral: it must persist to be paid).

    A chat's goes to the chat, in ``thread_id``; a personal one to the person's private chat, never a group.
    """
    return await bot.send_invoice(
        chat_id=owner_id,
        message_thread_id=thread_id if kind == "chat" else None,
        **_pack_invoice(locale, pack, kind, owner_id, thread_id),
    )


async def plan_link(bot: Bot, settings: Settings, locale: str, user_id: int) -> str:
    """A fresh Derp+ subscription link (a new payload nonce each time)."""
    offer = settings.plan
    title = t(locale, "billing.plan_title")
    description = t(locale, "billing.plan_description", credits=f"{offer.credits:,}")
    return await bot.create_invoice_link(
        **_invoice(title, description, plan_order(settings, user_id), offer.stars),
        subscription_period=offer.period_seconds,
    )


async def links_keyboard(
    bot: Bot,
    settings: Settings,
    locale: str,
    *,
    kind: str,
    owner_id: int,
    thread_id: int | None = None,
    plan: bool = False,
) -> InlineKeyboardMarkup | None:
    """Invoice-link buttons for each pack (and Derp+ for a person without one); raises TelegramAPIError."""
    links = await asyncio.gather(
        *(
            bot.create_invoice_link(**_pack_invoice(locale, pack, kind, owner_id, thread_id))
            for pack in settings.packs
        )
    )
    rows: list[InlineKeyboardButton] = [
        link(pack_label(pack), url) for pack, url in zip(settings.packs, links, strict=True)
    ]
    if plan:
        offer = settings.plan
        label = t(locale, "billing.plan_button", credits=f"{offer.credits:,}", stars=offer.stars)
        url = await plan_link(bot, settings, locale, owner_id)
        rows.insert(0, InlineKeyboardButton(text=label, url=url, style="primary"))
    return markup(*rows)


async def wallet_balance(db: Database, kind: str, owner_id: int) -> int:
    async with db.transaction() as session:
        return await balance(session, (await wallet(session, kind, owner_id)).id)


async def personal_menu(
    db: Database, settings: Settings, user_id: int, locale: str, extra: Row = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The private buy menu: the person's balance, Derp+ and the packs (then the ``extra`` row)."""
    mine = await wallet_balance(db, "user", user_id)
    keyboard = packs_keyboard(settings, "user", locale, extra, plan=True)
    return t(locale, "billing.menu", balance=f"{mine:,}"), keyboard


async def link_menu(
    db: Database,
    bot: Bot,
    settings: Settings,
    locale: str,
    *,
    kind: str,
    owner_id: int,
    thread_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The pack menu with invoice links: a chat's (anyone may pay) or a person's (Derp+ unless running)."""
    left = await wallet_balance(db, kind, owner_id)
    plan = False
    if kind == "user":
        async with db.session() as session:
            plan = await current_plan(session, owner_id) is None
    keyboard = await links_keyboard(
        bot, settings, locale, kind=kind, owner_id=owner_id, thread_id=thread_id, plan=plan
    )
    key = "billing.menu_chat" if kind == "chat" else "billing.menu"
    return t(locale, key, balance=f"{left:,}"), keyboard


def _thread(message: Message) -> int | None:
    return message.message_thread_id if message.is_topic_message else None


def _is_chooser(message: Message) -> bool:
    rows = message.reply_markup.inline_keyboard if message.reply_markup else []
    data = {b.callback_data for row in rows for b in row}
    return {BuyAction(target="chat").pack(), BuyAction(target="user").pack()} <= data


async def open_menu(press: Press, db: Database, settings: Settings, *, kind: str) -> None:
    """Show a group's presser the chat's or their own pack menu, visible only to them.

    A personal balance never shows in the group: if the ephemeral menu is refused, the personal one goes
    to the private chat. A refused chat menu is shown to everyone instead (chat balances are public).
    """
    bot, message = press.bot, press.message
    thread = _thread(message)
    owner = press.chat_id if kind == "chat" else press.user_id
    try:
        text, keyboard = await link_menu(
            db, bot, settings, press.locale, kind=kind, owner_id=owner, thread_id=thread
        )
    except TelegramAPIError:
        return await press.deny("billing.unavailable")
    target = Target.answering_press(press.chat_id, thread, press.user_id, press.query.id)
    try:
        await send_markdown(bot, target, text, reply_markup=keyboard)
        return None
    except TelegramBadRequest:
        pass  # expired, or not allowed here: fall back to a menu that needs no ephemeral message
    if kind == "user":
        text, personal = await personal_menu(db, settings, press.user_id, press.locale)
        delivered = await send_privately(bot, press.user_id, text, reply_markup=personal)
        return await press.deny("money.sent_privately" if delivered else "money.dm_first")
    if _is_chooser(message):
        await press.edit(text, keyboard)
        return None
    try:
        await send_markdown(bot, Target(press.chat_id, thread), text, reply_markup=keyboard)
    except TelegramAPIError:
        return await press.deny("billing.unavailable")
    return None


def top_up_button(locale: str, key: str = "billing.top_up") -> InlineKeyboardButton:
    """Opens the chat's pack menu for whoever presses it."""
    return button(t(locale, key), BuyAction(target="chat"))


def buy_button(locale: str, *, group: bool) -> InlineKeyboardButton:
    """Where someone ran short: topping up the chat in groups, the person's own buy menu in private."""
    return top_up_button(locale) if group else button(t(locale, "billing.buy"), BuyAction(target="user"))
