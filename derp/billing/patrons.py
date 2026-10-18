"""Patrons: the thank-you post when someone buys Derpcoins for a chat, and who chipped in lately.

One silent post goes to the chat (and topic) the purchase was made for: the buyer's name (or "a
mysterious patron"), the exact amount, and the wallet's runway at the chat's pace. Another chat
purchase within ``COALESCE`` edits that post instead of adding one. A post lists the paid purchases
that point at it (``purchases.patron_post_id``), so a refund drops out of its next edit, and names
each buyer as they chose (``users.patron_named``). The buyer alone may hide their name for
``HIDE_WINDOW``; that also makes their future chat purchases anonymous. State lives in rows, so the
buttons survive deploys. Never pinned.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.menu import top_up_button
from derp.billing.models import PatronPost, Purchase
from derp.billing.runway import daily_pace, lasts
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.money.funding import Funder, Funding
from derp.money.models import Wallet
from derp.money.wallets import balance, wallet
from derp.settings.panel import apply_setting
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, rich, send_markdown
from derp.telegram.text import literal

COALESCE = timedelta(minutes=30)
HIDE_WINDOW = timedelta(minutes=10)
LATELY_DAYS = 30
LATELY_SHOWN = 3
HYPES = 3  # rotating wordings, chosen per post so edits keep theirs

_posting = asyncio.Lock()  # one post or edit at a time: coalescing reads, then writes, the latest post


class PatronAction(CallbackData, prefix="patron"):
    post: int  # hide the presser's name on this post


async def runway(
    session: AsyncSession, locale: str, wallet_id: int, left: int, turn_price: int, now: datetime
) -> str:
    """The wallet's balance and how long it lasts at the chat's recent pace (or in answers, early on)."""
    amount = f"{left:,}"
    if (pace := await daily_pace(session, wallet_id, now)) is not None:
        return t(locale, "patron.runway", balance=amount, lasts=lasts(locale, left, pace))
    answers = left // max(1, turn_price)
    if answers < 1:
        return t(locale, "patron.runway_none", balance=amount)
    return t(locale, "patron.runway_answers", balance=amount, answers=f"{answers:,}")


def _name(user: User | None, locale: str) -> str:
    return (
        f"**{literal(one_line(user.full_name))}**" if user and user.full_name else t(locale, "patron.someone")
    )


def _who(locale: str, names: list[str], anonymous: int) -> str:
    parts = list(names)
    if anonymous:
        parts.append(
            t(locale, "patron.mystery") if anonymous == 1 else t(locale, "patron.mysteries", count=anonymous)
        )
    who = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + t(locale, "patron.and") + parts[-1]
    return who[0].upper() + who[1:] if who[0].islower() else who


async def _bought(session: AsyncSession, post_id: int) -> list[tuple[Purchase, User | None]]:
    """The purchases a post thanks for, in order, each with its buyer; refunded ones drop out."""
    rows = await session.execute(
        select(Purchase, User)
        .outerjoin(User, User.id == Purchase.user_id)
        .where(Purchase.patron_post_id == post_id, Purchase.status == "paid")
        .order_by(Purchase.id)
    )
    return [(purchase, buyer) for purchase, buyer in rows]


def _named(buyer: User | None) -> bool:
    return buyer is None or buyer.patron_named


def _hide_until(purchases: list[tuple[Purchase, User | None]], user: int | None = None) -> datetime | None:
    """``HIDE_WINDOW`` after the latest purchase of a named buyer (``user``, if given); None when nobody is named."""
    times = [p.created_at for p, buyer in purchases if _named(buyer) and user in (None, p.user_id)]
    return max(times) + HIDE_WINDOW if times else None


def keyboard(post: PatronPost, now: datetime) -> InlineKeyboardMarkup | None:
    hide = post.hide_until is not None and post.hide_until > now
    return markup(
        [
            top_up_button(post.locale, key="patron.chip_in"),
            button(t(post.locale, "patron.hide"), PatronAction(post=post.id)) if hide else None,
        ]
    )


async def _refresh(
    session: AsyncSession, post: PatronPost, funder: Funder, now: datetime
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The post as it stands now (its purchases, names and runway) and its buttons."""
    purchases = await _bought(session, post.id)
    post.hide_until = _hide_until(purchases)
    text = await render(session, post, purchases, await funder.typical_turn(session, Funding.PAID), now)
    return text, keyboard(post, now)


async def render(
    session: AsyncSession,
    post: PatronPost,
    purchases: list[tuple[Purchase, User | None]],
    turn_price: int,
    now: datetime,
) -> str:
    """The post: who chipped in (named as each buyer chose; in order of their first purchase), how much, runway."""
    locale = post.locale
    buyers = {p.user_id: buyer for p, buyer in purchases}
    names = [_name(buyer, locale) for buyer in buyers.values() if _named(buyer)]
    who = _who(locale, names, sum(1 for buyer in buyers.values() if not _named(buyer)))
    chat_wallet = await wallet(session, "chat", post.chat_id)
    left = await balance(session, chat_wallet.id, now=now)
    line = await runway(session, locale, chat_wallet.id, left, turn_price, now)
    variant = f"patron.hype_{post.id % HYPES + 1}"
    total = sum(p.credits for p, _ in purchases)
    return t(locale, variant, who=who, credits=f"{total:,}", runway=line)


async def thank(
    db: Database,
    bot: Bot,
    funder: Funder,
    *,
    purchase_id: int,
    chat_id: int,
    thread_id: int | None,
    locale: str,
) -> None:
    """Post (or extend the chat's recent) thank-you post for a chat purchase; best effort."""
    async with _posting:
        now = datetime.now(UTC)
        async with db.transaction() as session:
            chat = await session.get(Chat, chat_id)
            post = await session.scalar(
                select(PatronPost)
                .where(
                    PatronPost.chat_id == chat_id,
                    PatronPost.thread_id.is_(None)
                    if thread_id is None
                    else PatronPost.thread_id == thread_id,
                    PatronPost.message_id.is_not(None),
                    PatronPost.created_at >= now - COALESCE,
                )
                .order_by(PatronPost.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if post is None:
                post = PatronPost(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    locale=resolve(chat.locale if chat and chat.locale else locale),
                )
                session.add(post)
                await session.flush()
            await session.execute(
                update(Purchase).where(Purchase.id == purchase_id).values(patron_post_id=post.id)
            )
            text, buttons = await _refresh(session, post, funder, now)
            post_id, message_id = post.id, post.message_id
        if message_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, rich_message=rich(text), reply_markup=buttons
                )
                return
            except TelegramAPIError:
                pass  # deleted meanwhile: post it afresh
        sent: list[Message] = []
        for thread in dict.fromkeys(
            (thread_id, None)
        ):  # a topic closed or deleted meanwhile: the general one
            try:
                sent = await send_markdown(
                    bot, Target(chat_id, thread), text, reply_markup=buttons, silent=True
                )
            except TelegramBadRequest:
                continue  # nothing was delivered
            except TelegramAPIError:
                break
            break
        async with db.transaction() as session:
            stored = await session.get(PatronPost, post_id)
            if stored is not None and sent:
                stored.message_id = sent[-1].message_id
                stored.thread_id = sent[-1].message_thread_id if sent[-1].is_topic_message else None
            elif stored is not None and message_id is None:
                await session.delete(stored)  # never reached the chat: nothing to coalesce into


async def lately(session: AsyncSession, chat_id: int, now: datetime) -> list[str]:
    """Named people who bought the most Derpcoins for this chat lately (refunds excluded)."""
    total = func.sum(Purchase.credits)
    rows = await session.execute(
        select(User, total)
        .join(User, User.id == Purchase.user_id)
        .join(Wallet, Wallet.id == Purchase.wallet_id)
        .where(
            Wallet.kind == "chat",
            Wallet.owner_id == chat_id,
            Purchase.status == "paid",
            Purchase.created_at >= now - timedelta(days=LATELY_DAYS),
            User.patron_named,
        )
        .group_by(User.id)
        .order_by(total.desc(), User.id)
        .limit(LATELY_SHOWN)
    )
    return [name for user, _ in rows if (name := one_line(user.full_name))]


async def lately_line(db: Database, chat_id: int, locale: str) -> str | None:
    async with db.session() as session:
        names = await lately(session, chat_id, datetime.now(UTC))
    return t(locale, "patron.lately", names=", ".join(map(literal, names))) if names else None


async def expire_hide_buttons(db: Database, bot: Bot) -> int:
    """Drop "hide my name" from posts whose window passed; returns how many were edited."""
    now = datetime.now(UTC)
    async with db.transaction() as session:
        due = list(
            await session.scalars(
                select(PatronPost).where(PatronPost.hide_until <= now).with_for_update(skip_locked=True)
            )
        )
        for post in due:
            post.hide_until = None
        edits = [(p.chat_id, p.message_id, keyboard(p, now)) for p in due if p.message_id is not None]
    for chat_id, message_id, buttons in edits:
        with contextlib.suppress(TelegramAPIError):
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=buttons)
    return len(edits)


def build_router() -> Router:
    router = Router(name="patrons")

    @on_press(router, PatronAction)
    async def on_hide(
        press: Press, data: PatronAction, db: Database, settings: Settings, funder: Funder
    ) -> None:
        """The buyer hides their name on the post (and in future chat purchases); others are refused.

        A refusal leaves the button alone: other buyers may still be in their window, and the sweep
        (``expire_hide_buttons``) drops it once nobody is.
        """
        async with _posting:
            now = datetime.now(UTC)
            async with db.session() as session:
                post = await session.get(PatronPost, data.post)
                purchases = [] if post is None else await _bought(session, post.id)
            if post is None or post.chat_id != press.chat_id:
                return await press.deny("buttons.stale")
            if not any(p.user_id == press.user_id for p, _ in purchases):
                return await press.deny("patron.not_yours")
            if (own := _hide_until(purchases, press.user_id)) is None or own <= now:
                return await press.deny("patron.too_late")  # already anonymous here, or too late
            await apply_setting(db, settings, press.chat_id, press.user_id, "named", "off")
            async with db.transaction() as session:
                post = await session.get(PatronPost, data.post, with_for_update=True)
                if post is None:
                    return await press.deny("buttons.stale")
                text, buttons = await _refresh(session, post, funder, now)
            await press.edit(text, buttons)
            return await press.toast("patron.hidden")

    return router
