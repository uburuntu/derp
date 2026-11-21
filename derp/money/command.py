"""/credits and /info: balances and per-answer receipts, shown only to the person asking.

The /info receipt carries Report a problem (``ops.feedback``), which files a report on that answer.

In groups both are ephemeral commands, so replies are ephemeral too (see
``Target``); a command sent the ordinary way is answered by DM instead. Admins
also see who spent the chat's Derpcoins in the last 30 days.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import Bot, Router
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.patrons import lately_line
from derp.billing.purchases import current_plan
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.history.render import one_line
from derp.keys.notices import brand, owner_name
from derp.locales import resolve, t
from derp.memory.notes import is_admin
from derp.money.accounting import Receipt, receipts, usage
from derp.money.amounts import Amount, format_dc
from derp.money.funding import day_end, sponsored_lot_id
from derp.money.models import CreditLot, Wallet
from derp.money.wallets import balances, wallet
from derp.ops.feedback import report_button
from derp.telegram.buttons import markup
from derp.telegram.filters import Command
from derp.telegram.sender import Target, send_markdown, send_privately
from derp.telegram.text import literal

USAGE_DAYS = 30
USAGE_ROWS = 10


async def _reply_privately(
    bot: Bot, message: Message, locale: str, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    target = Target.reply_to_message(message)
    if message.chat.type == "private" or message.ephemeral_message_id or message.from_user is None:
        await send_markdown(bot, target, text, reply_markup=keyboard)
        return
    delivered = await send_privately(bot, message.from_user.id, text, reply_markup=keyboard)
    await send_markdown(bot, target, t(locale, "money.sent_privately" if delivered else "money.dm_first"))


async def show_credits(message: Message, db: Database, settings: Settings, bot: Bot, chat_row: Chat) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    text = await credits_card(db, settings, bot, chat_row, message.from_user.id, locale)
    await _reply_privately(bot, message, locale, text)


def bought_total(balances_by_source: dict[str, Decimal]) -> Amount:
    """Derpcoins that never expire: bought, granted or carried over from the previous Derp."""
    return sum(balances_by_source.get(source, 0) for source in ("purchase", "grant", "migration"))


async def free_left_today(session: AsyncSession, settings: Settings, user_id: int, now: datetime) -> Amount:
    today = await session.scalar(
        select(CreditLot.remaining).where(CreditLot.operation_id == sponsored_lot_id(user_id, now))
    )
    return settings.sponsored_daily_credits if today is None else today  # the day's lot appears on first use


async def credits_card(
    db: Database, settings: Settings, bot: Bot, chat_row: Chat, user_id: int, locale: str
) -> str:
    """The person's balances (and the chat's, with its spenders for admins): private information."""
    now = datetime.now(UTC)
    chat_left: Amount | None = None
    async with db.transaction() as session:
        mine = await balances(session, (await wallet(session, "user", user_id)).id, now=now)
        plan = await current_plan(session, user_id, now=now)
        free = await free_left_today(session, settings, user_id, now)
        chat_wallet: Wallet | None = None
        if chat_row.type != "private":
            chat_wallet = await wallet(session, "chat", chat_row.id)
            chat_left = sum((await balances(session, chat_wallet.id, now=now)).values())
    lines = [t(locale, "credits.title")]
    if plan is not None:
        left = format_dc(mine.get("plan", 0))
        lines.append(t(locale, "credits.plan", left=left, date=f"{plan.period_end:%Y-%m-%d}"))
    bought = bought_total(mine)
    lines.append(t(locale, "credits.bought", left=format_dc(bought)))
    lines.append(
        t(
            locale,
            "credits.free",
            left=format_dc(free),
            daily=format_dc(settings.sponsored_daily_credits),
            time=f"{day_end(now):%H:%M} UTC",
        )
    )
    if chat_wallet is not None and chat_left is not None:
        lines.append(t(locale, "credits.chat", left=format_dc(chat_left)))
        if patrons := await lately_line(db, chat_row.id, locale):
            lines.append(patrons)
        if await is_admin(bot, chat_row, user_id):
            lines.extend(await _chat_usage(db, chat_row, locale))
    lines.append(t(locale, "credits.hint"))
    return "\n".join(lines)


async def _chat_usage(db: Database, chat: Chat, locale: str) -> list[str]:
    async with db.session() as session:
        # Historical run timestamps and stand-alone provider calls use the database clock.
        now = (await session.execute(select(func.now()))).scalar_one()
        report = await usage(
            session, chat, now - timedelta(days=USAGE_DAYS), now, group_by="member", funding="chat_wallet"
        )
    spenders = [row for row in report["groups_top_50"] if row["wallet_dc"] != "0"]
    if not spenders:
        return [t(locale, "credits.usage_none", days=USAGE_DAYS)]
    lines = [f"- {literal(one_line(row['member']))} — {row['wallet_dc']}" for row in spenders[:USAGE_ROWS]]
    return [t(locale, "credits.usage", days=USAGE_DAYS), *lines]


def receipt_text(receipt: Receipt, locale: str) -> str:
    run = receipt.run
    funding = {
        "paid": "info.by_key"
        if receipt.key and not receipt.wallet_kind
        else f"info.paid_{receipt.wallet_kind or 'user'}",
        "sponsored": "info.sponsored",
        "free": "info.free",
    }
    text = t(
        locale,
        "info.receipt",
        model=run.served_model or "—",
        input=f"{run.input_tokens:,}",
        output=f"{run.output_tokens:,}",
        context=f"{run.context_tokens:,}",
        tools=run.tool_calls,
        charged=format_dc(run.charged_credits),
        funding=t(locale, funding.get(run.funding, "info.free")),
    )
    text += "\n" + t(
        locale,
        "info.costs",
        cached=f"{run.cache_read_tokens:,}",
        provider=format_dc(receipt.provider_usd),
        fees=format_dc(receipt.fee_dc),
    )
    if receipt.key:
        text += "\n" + t(
            locale,
            "info.key",
            who=owner_name(receipt.contributor, locale),
            brand=brand(receipt.key.provider),
            usd=format_dc(receipt.upstream_usd),
        )
    return text


async def show_info(message: Message, db: Database, bot: Bot) -> None:
    """One receipt per version of the delivered message, with reporting tied to the latest version."""
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    replied = message.reply_to_message
    async with db.session() as session:
        entries = await receipts(session, message.chat.id, replied.message_id) if replied else []
    if not entries or replied is None:
        await send_markdown(bot, Target.reply_to_message(message), t(locale, "info.how"))
        return
    text = "\n\n".join(
        (t(locale, "info.revision") + "\n" if entry.revision else "") + receipt_text(entry, locale)
        for entry in entries
    )
    report = markup(report_button(locale, entries[-1].run.id, replied.message_id))
    await _reply_privately(bot, message, locale, text, report)


def build_router() -> Router:
    router = Router(name="money")
    router.message.register(show_credits, Command("credits"))
    router.message.register(show_info, Command("info"))
    return router
