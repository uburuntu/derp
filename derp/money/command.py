"""/credits and /info: balances and per-answer receipts, shown only to the person asking.

In groups both are ephemeral commands, so replies are ephemeral too (see
``Target``); a command sent the ordinary way is answered by DM instead. Admins
also see who spent the chat's Derpcoins in the last 30 days.
"""

from datetime import UTC, datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.agent.runs import run_for_output
from derp.billing.purchases import current_plan
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.memory.notes import is_admin
from derp.money.funding import day_end, sponsored_lot_id
from derp.money.models import CreditLot, Wallet
from derp.money.wallets import balances, wallet
from derp.telegram.sender import Target, send_markdown, send_privately

USAGE_DAYS = 30
USAGE_ROWS = 10


async def _reply_privately(bot: Bot, message: Message, locale: str, text: str) -> None:
    target = Target.reply_to_message(message)
    if message.chat.type == "private" or message.ephemeral_message_id or message.from_user is None:
        await send_markdown(bot, target, text)
        return
    delivered = await send_privately(bot, message.from_user.id, text)
    await send_markdown(bot, target, t(locale, "money.sent_privately" if delivered else "money.dm_first"))


async def show_credits(message: Message, db: Database, settings: Settings, bot: Bot, chat_row: Chat) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    text = await credits_card(db, settings, bot, chat_row, message.from_user.id, locale)
    await _reply_privately(bot, message, locale, text)


async def free_left_today(session: AsyncSession, settings: Settings, user_id: int, now: datetime) -> int:
    today = await session.scalar(
        select(CreditLot.remaining).where(CreditLot.operation_id == sponsored_lot_id(user_id, now))
    )
    return settings.sponsored_daily_credits if today is None else today  # the day's lot appears on first use


async def credits_card(
    db: Database, settings: Settings, bot: Bot, chat_row: Chat, user_id: int, locale: str
) -> str:
    """The person's balances (and the chat's, with its spenders for admins): private information."""
    now = datetime.now(UTC)
    chat_left: int | None = None
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
        left = f"{mine.get('plan', 0):,}"
        lines.append(t(locale, "credits.plan", left=left, date=f"{plan.period_end:%Y-%m-%d}"))
    bought = mine.get("purchase", 0) + mine.get("grant", 0) + mine.get("migration", 0)
    lines.append(t(locale, "credits.bought", left=f"{bought:,}"))
    lines.append(
        t(
            locale,
            "credits.free",
            left=free,
            daily=settings.sponsored_daily_credits,
            time=f"{day_end(now):%H:%M} UTC",
        )
    )
    if chat_wallet is not None and chat_left is not None:
        lines.append(t(locale, "credits.chat", left=f"{chat_left:,}"))
        if await is_admin(bot, chat_row, user_id):
            lines.extend(await _chat_usage(db, chat_wallet.id, locale, now))
    lines.append(t(locale, "credits.hint"))
    return "\n".join(lines)


async def _chat_usage(db: Database, wallet_id: int, locale: str, now: datetime) -> list[str]:
    since = now - timedelta(days=USAGE_DAYS)
    spent = func.sum(AgentRun.charged_credits)
    async with db.session() as session:
        rows = (
            await session.execute(
                select(User.first_name, User.last_name, spent)
                .join(User, User.id == AgentRun.user_id)
                .where(
                    AgentRun.wallet_id == wallet_id,
                    AgentRun.created_at >= since,
                    AgentRun.charged_credits > 0,
                )
                .group_by(User.id, User.first_name, User.last_name)
                .order_by(spent.desc())
                .limit(USAGE_ROWS)
            )
        ).all()
    if not rows:
        return [t(locale, "credits.usage_none", days=USAGE_DAYS)]
    lines = [f"- {one_line(' '.join(p for p in (r[0], r[1]) if p))} — {int(r[2]):,}" for r in rows]
    return [t(locale, "credits.usage", days=USAGE_DAYS), *lines]


async def show_info(message: Message, db: Database, bot: Bot) -> None:
    """Reply to one of Derp's messages: which model answered, the tokens, and what it cost."""
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    replied = message.reply_to_message
    run: AgentRun | None = None
    payer: str | None = None
    if replied is not None:
        async with db.session() as session:
            run = await run_for_output(session, message.chat.id, replied.message_id)
            if run is not None and run.wallet_id is not None:
                payer = await session.scalar(select(Wallet.kind).where(Wallet.id == run.wallet_id))
    if run is None:
        await send_markdown(bot, Target.reply_to_message(message), t(locale, "info.how"))
        return
    funding = {"paid": f"info.paid_{payer or 'user'}", "sponsored": "info.sponsored", "free": "info.free"}
    text = t(
        locale,
        "info.receipt",
        model=run.served_model or "—",
        input=f"{run.input_tokens:,}",
        output=f"{run.output_tokens:,}",
        context=f"{run.context_tokens:,}",
        tools=run.tool_calls,
        charged=f"{run.charged_credits:,}",
        funding=t(locale, funding.get(run.funding, "info.free")),
    )
    await _reply_privately(bot, message, locale, text)


def build_router() -> Router:
    router = Router(name="money")
    router.message.register(show_credits, Command("credits"))
    router.message.register(show_info, Command("info"))
    return router
