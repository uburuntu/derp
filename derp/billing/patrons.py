"""Patrons: the thank-you post when someone buys Derpcoins for a chat, and who chipped in lately.

One silent post goes to the chat (and topic) the purchase was made for: the buyer's name (or "a
mysterious patron"), the exact amount, and the wallet's runway at the chat's pace. Another chat
purchase within ``COALESCE`` edits that post instead of adding one. The buyer alone may hide their
name for ``HIDE_WINDOW``; that also makes their future chat purchases anonymous (``users.patron_named``).
State lives in ``patron_posts``, so the buttons survive deploys. Never pinned.
"""

import asyncio
import contextlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.billing.menu import top_up_button
from derp.billing.models import PatronPost, Purchase
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.money.funding import Funder, Funding
from derp.money.models import Wallet
from derp.money.wallets import balance, wallet
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, rich, send_markdown
from derp.telegram.text import literal

COALESCE = timedelta(minutes=30)
HIDE_WINDOW = timedelta(minutes=10)
PACE_DAYS = 14  # runway: the wallet's average daily spend over this many days
MIN_ACTIVE_DAYS = 3  # fewer days with spending: runway in answers at a typical turn's price
MONTH_DAYS = 30
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
    charged = await session.execute(
        select(
            func.coalesce(func.sum(AgentRun.charged_credits), 0),
            func.count(func.distinct(cast(func.timezone("UTC", AgentRun.created_at), Date))),
            func.min(AgentRun.created_at),
        ).where(
            AgentRun.wallet_id == wallet_id,
            AgentRun.created_at >= now - timedelta(days=PACE_DAYS),
            AgentRun.charged_credits > 0,
        )
    )
    spent, active, first = charged.one()
    amount = f"{left:,}"
    if active >= MIN_ACTIVE_DAYS and first is not None and spent > 0:
        # Average over the days since spending began in the window (a young wallet isn't diluted).
        span = min(PACE_DAYS, max(active, math.ceil((now - first).total_seconds() / 86400)))
        days = left / (int(spent) / span)
        if days > MONTH_DAYS:
            return t(locale, "patron.runway_month", balance=amount)
        if days >= 2:
            return t(locale, "patron.runway_days", balance=amount, days=round(days))
        return t(locale, "patron.runway_day" if days >= 1 else "patron.runway_hours", balance=amount)
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


def _named_users(buyers: list[dict[str, Any]]) -> tuple[list[int], dict[int, bool]]:
    """Buyers in order of their first purchase, and whether each is named (their latest choice)."""
    order: list[int] = []
    named: dict[int, bool] = {}
    for entry in buyers:
        user = int(entry["user"])
        if user not in named:
            order.append(user)
        named[user] = bool(entry["named"])
    return order, named


def _hide_until(buyers: list[dict[str, Any]], user: int | None = None) -> datetime | None:
    """``HIDE_WINDOW`` after the latest named purchase (of ``user``, if given); None when nobody is named."""
    times = [int(e["at"]) for e in buyers if e["named"] and user in (None, int(e["user"]))]
    return datetime.fromtimestamp(max(times), UTC) + HIDE_WINDOW if times else None


def keyboard(post: PatronPost, now: datetime) -> InlineKeyboardMarkup | None:
    hide = post.hide_until is not None and post.hide_until > now
    return markup(
        [
            top_up_button(post.locale, key="patron.chip_in"),
            button(t(post.locale, "patron.hide"), PatronAction(post=post.id)) if hide else None,
        ]
    )


async def render(session: AsyncSession, post: PatronPost, turn_price: int, now: datetime) -> str:
    locale = post.locale
    order, named = _named_users(post.buyers)
    users = {u.id: u for u in await session.scalars(select(User).where(User.id.in_(order)))}
    names = [_name(users.get(user), locale) for user in order if named[user]]
    who = _who(locale, names, sum(1 for user in order if not named[user]))
    chat_wallet = await wallet(session, "chat", post.chat_id)
    left = await balance(session, chat_wallet.id, now=now)
    line = await runway(session, locale, chat_wallet.id, left, turn_price, now)
    variant = f"patron.hype_{post.id % HYPES + 1}"
    return t(locale, variant, who=who, credits=f"{post.credits:,}", runway=line)


async def thank(
    db: Database,
    bot: Bot,
    funder: Funder,
    *,
    chat_id: int,
    thread_id: int | None,
    buyer_id: int,
    amount: int,
    locale: str,
) -> None:
    """Post (or extend the chat's recent) thank-you post for a chat purchase; best effort."""
    async with _posting:
        now = datetime.now(UTC)
        async with db.transaction() as session:
            chat = await session.get(Chat, chat_id)
            buyer = await session.get(User, buyer_id)
            entry = {
                "user": buyer_id,
                "credits": amount,
                "named": buyer.patron_named if buyer else True,
                "at": int(now.timestamp()),
            }
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
                    credits=amount,
                    buyers=[entry],
                )
                session.add(post)
            else:
                post.buyers = [*post.buyers, entry]
                post.credits += amount
            post.hide_until = _hide_until(post.buyers)
            await session.flush()
            text = await render(session, post, await funder.typical_turn(session, Funding.PAID), now)
            buttons = keyboard(post, now)
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
    """Named people who bought the most Derpcoins for this chat lately (refunds excluded), escaped."""
    total = func.sum(Purchase.credits)
    rows = await session.execute(
        select(User.first_name, User.last_name, total)
        .join(User, User.id == Purchase.user_id)
        .join(Wallet, Wallet.id == Purchase.wallet_id)
        .where(
            Wallet.kind == "chat",
            Wallet.owner_id == chat_id,
            Purchase.status == "paid",
            Purchase.created_at >= now - timedelta(days=LATELY_DAYS),
            User.patron_named,
        )
        .group_by(User.id, User.first_name, User.last_name)
        .order_by(total.desc(), User.id)
        .limit(LATELY_SHOWN)
    )
    names = (" ".join(part for part in (first, last) if part) for first, last, _ in rows)
    return [literal(one_line(name)) for name in names if name]


async def lately_line(db: Database, chat_id: int, locale: str) -> str | None:
    async with db.session() as session:
        names = await lately(session, chat_id, datetime.now(UTC))
    return t(locale, "patron.lately", names=", ".join(names)) if names else None


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
    async def on_hide(press: Press, data: PatronAction, db: Database, funder: Funder) -> None:
        """The buyer hides their name on the post (and in future chat purchases); others are refused."""
        async with _posting:
            now = datetime.now(UTC)
            refusal: str | None = None
            text = ""
            buttons: InlineKeyboardMarkup | None = None
            async with db.transaction() as session:
                post = await session.get(PatronPost, data.post, with_for_update=True)
                mine = [] if post is None else [e for e in post.buyers if int(e["user"]) == press.user_id]
                if post is None or post.chat_id != press.chat_id:
                    refusal = "buttons.stale"
                elif not mine:
                    refusal = "patron.not_yours"
                elif (own := _hide_until(post.buyers, press.user_id)) is None or own <= now:
                    refusal = "patron.too_late"  # already anonymous here, or too late
                else:
                    post.buyers = [
                        {**e, "named": False} if int(e["user"]) == press.user_id else e for e in post.buyers
                    ]
                    post.hide_until = _hide_until(post.buyers)
                    person = await session.get(User, press.user_id)
                    if person is not None:
                        person.patron_named = False
                    text = await render(session, post, await funder.typical_turn(session, Funding.PAID), now)
                if post is not None and refusal != "buttons.stale":
                    buttons = keyboard(post, now)
            if refusal == "patron.too_late":
                await press.strip(lambda b: b.callback_data != data.pack())
            if refusal is not None:
                return await press.deny(refusal)
            await press.edit(text, buttons)
            await press.toast("patron.hidden")
            return None

    return router


async def set_named(db: Database, user_id: int, *, named: bool) -> None:
    """The personal "name me when I chip in" choice (``users.patron_named``), for Derp and the settings panel."""
    async with db.transaction() as session:
        await session.execute(update(User).where(User.id == user_id).values(patron_named=named))
