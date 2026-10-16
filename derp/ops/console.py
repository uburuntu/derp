"""Owner console, unlisted and private-chat only: /stats, /grant, /block, /unblock.

/stats shows provider spend (the provider-call ledger only; Derp's own fees are
counted apart), Stars revenue, runs, the busiest chats, the OpenRouter key's
remaining limit, the live model pins (with Roll back) and the model scout's
open proposals (Apply / Ignore). Anyone else's command falls through to the
unknown-command reply, so the console doesn't announce itself. Owner copy is
plain English, not localized.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, Message
from sqlalchemy import distinct, func, select

from derp.agent.models import AgentRun
from derp.billing.models import Purchase
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.models import ModelPin, ProviderCall, ScoutProposal
from derp.inference.pins import PinStore, apply_pin, rollback_pin
from derp.inference.roles import ModelRole, RolePin
from derp.locales import resolve, t
from derp.money.models import RunFee
from derp.money.wallets import balance, credit, wallet
from derp.telegram.sender import Target, send_markdown, send_privately

WINDOWS = ((1, "24 h"), (7, "7 d"), (30, "30 d"))
FUNDINGS = ("paid", "sponsored", "free", "creator")
TOP_CHATS = 5
MAX_GRANT = 1_000_000


class ConsoleAction(CallbackData, prefix="own"):
    action: str  # apply | ignore (a scout proposal) | rollback (a role's pin)
    key: str


async def from_owner(message: Message, settings: Settings) -> bool:
    return (
        message.chat.type == "private"
        and message.from_user is not None
        and message.from_user.id in settings.owner_ids
    )


def _usd(value: Decimal | float | None) -> str:
    return f"${float(value or 0):.2f}"


async def _key_line(api: OpenRouterAPI) -> str:
    try:
        key = await api.key_info()
    except OpenRouterError:
        return "OpenRouter key: unavailable"
    if key.get("limit_remaining") is None:
        return f"OpenRouter key: {_usd(key.get('usage'))} used, no limit"
    return f"OpenRouter key: {_usd(key['limit_remaining'])} left of {_usd(key.get('limit'))}"


async def stats(db: Database, api: OpenRouterAPI) -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(UTC)
    week = now - timedelta(days=7)
    cost, stars, refunds, runs = [], [], [], []
    async with db.session() as session:
        for days, _ in WINDOWS:
            since = now - timedelta(days=days)
            cost.append(
                await session.scalar(
                    select(func.sum(ProviderCall.cost_usd)).where(ProviderCall.created_at >= since)
                )
            )
            stars.append(
                await session.scalar(
                    select(func.sum(Purchase.stars)).where(
                        Purchase.created_at >= since, Purchase.status == "paid"
                    )
                )
            )
            refunds.append(await session.scalar(select(func.count()).where(Purchase.refunded_at >= since)))
            runs.append(await session.scalar(select(func.count()).where(AgentRun.created_at >= since)))
        rows = await session.execute(
            select(ProviderCall.funding, func.sum(ProviderCall.cost_usd))
            .where(ProviderCall.created_at >= week)
            .group_by(ProviderCall.funding)
        )
        by_funding: dict[str, Decimal] = {row[0]: row[1] for row in rows}
        fees = await session.scalar(select(func.sum(RunFee.credits)).where(RunFee.created_at >= week))
        people, chats = (
            await session.execute(
                select(func.count(distinct(AgentRun.user_id)), func.count(distinct(AgentRun.chat_id))).where(
                    AgentRun.created_at >= week
                )
            )
        ).one()
        spent = func.sum(ProviderCall.cost_usd)
        top = (
            await session.execute(
                select(ProviderCall.chat_id, Chat.title, spent)
                .outerjoin(Chat, Chat.id == ProviderCall.chat_id)
                .where(ProviderCall.created_at >= week, ProviderCall.chat_id.is_not(None))
                .group_by(ProviderCall.chat_id, Chat.title)
                .order_by(spent.desc())
                .limit(TOP_CHATS)
            )
        ).all()
        pins = list(await session.scalars(select(ModelPin).order_by(ModelPin.role)))
        proposals = list(
            await session.scalars(
                select(ScoutProposal).where(ScoutProposal.status == "open").order_by(ScoutProposal.id)
            )
        )
        blocked = await session.scalar(select(func.count()).where(User.blocked_at.is_not(None)))
        blocked_chats = await session.scalar(select(func.count()).where(Chat.blocked_at.is_not(None)))

    spans = " · ".join(label for _, label in WINDOWS)
    lines = [
        "📊 **Derp stats**",
        f"Provider cost ({spans}): " + " · ".join(_usd(c) for c in cost),
        "7 d by funding: " + " · ".join(f"{f} {_usd(by_funding.get(f))}" for f in FUNDINGS),
        f"Revenue ({spans}): " + " · ".join(f"{s or 0}⭐" for s in stars),
        f"Refunds ({spans}): " + " · ".join(str(r or 0) for r in refunds),
        f"Derp fees (7 d): {fees or 0} dc",
        f"Runs ({spans}): "
        + " · ".join(str(r or 0) for r in runs)
        + f"; 7 d: {people} people in {chats} chats",
        f"Blocked: {blocked or 0} people, {blocked_chats or 0} chats",
        await _key_line(api),
    ]
    if top:
        lines.append("**Top chats by cost (7 d)**")
        lines.extend(f"- {one_line(title or '—')} (`{chat_id}`) — {_usd(c)}" for chat_id, title, c in top)
    buttons: list[list[InlineKeyboardButton]] = []
    if pins:
        lines.append("**Pins**")
        for pin in pins:
            lines.append(f"- {pin.role}: `{pin.pin.get('model')}` ({pin.source})")
            if pin.previous:
                label = f"Roll back {pin.role} → {pin.previous.get('model')}"
                buttons.append([_button(label, "rollback", pin.role)])
    if proposals:
        lines.append("**Scout proposals**")
        for p in proposals:
            lines.append(
                f"- #{p.id} {p.role}: `{p.current.get('model')}` → `{p.proposed.get('model')}` — {p.reason}"
            )
            buttons.append(
                [
                    _button(f"Apply #{p.id}", "apply", str(p.id)),
                    _button(f"Ignore #{p.id}", "ignore", str(p.id)),
                ]
            )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _button(text: str, action: str, key: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=ConsoleAction(action=action, key=key).pack())


async def show_stats(message: Message, db: Database, api: OpenRouterAPI, bot: Bot) -> None:
    text, markup = await stats(db, api)
    await send_markdown(bot, Target.reply_to_message(message), text, reply_markup=markup)


async def decide(action: str, key: str, db: Database, pins: PinStore) -> str:
    now = datetime.now(UTC)
    async with db.transaction() as session:
        if action == "rollback":
            if key not in {role.value for role in ModelRole} or not await rollback_pin(
                session, ModelRole(key)
            ):
                return "Nothing to roll back."
            note = f"Rolled back {key}."
        else:
            proposal = await session.get(
                ScoutProposal, int(key) if key.isdigit() else 0, with_for_update=True
            )
            if proposal is None or proposal.status != "open":
                return "Already decided."
            if action == "apply":
                pin = RolePin.model_validate(proposal.proposed)
                await apply_pin(session, ModelRole(proposal.role), pin, source="owner")
            proposal.status = "applied" if action == "apply" else "ignored"
            proposal.decided_at = now
            note = f"Proposal #{proposal.id} {proposal.status}."
    await pins.refresh()
    return note


async def on_console(
    query: CallbackQuery,
    callback_data: ConsoleAction,
    db: Database,
    api: OpenRouterAPI,
    pins: PinStore,
    settings: Settings,
    bot: Bot,
) -> None:
    message = query.message
    if query.from_user.id not in settings.owner_ids or not isinstance(message, Message):
        await query.answer()
        return
    await query.answer(await decide(callback_data.action, callback_data.key, db, pins), show_alert=True)
    text, markup = await stats(db, api)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=message.message_id,
        rich_message=InputRichMessage(markdown=text),
        reply_markup=markup,
    )


async def grant(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    """``/grant <user or chat id> <Derpcoins>``: a gift lot (negative ids are chats). Repeats are idempotent."""
    target = Target.reply_to_message(message)
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
        await send_markdown(bot, target, "Usage: `/grant <user or chat id> <Derpcoins>`")
        return
    owner_id, amount = int(parts[0]), int(parts[1])
    if not 0 < amount <= MAX_GRANT:
        await send_markdown(bot, target, f"Grant between 1 and {MAX_GRANT:,} Derpcoins.")
        return
    kind = "chat" if owner_id < 0 else "user"
    async with db.transaction() as session:
        if await session.get(Chat if kind == "chat" else User, owner_id) is None:
            await send_markdown(bot, target, f"No {kind} `{owner_id}` known.")
            return
        wallet_id = (await wallet(session, kind, owner_id)).id
        await credit(session, wallet_id, f"grant:{message.chat.id}:{message.message_id}", amount, "grant")
        left = await balance(session, wallet_id)
        person = await session.get(User, owner_id) if kind == "user" else None
    note = ""
    if person is not None:
        locale = resolve(person.language_code)
        delivered = await send_privately(bot, owner_id, t(locale, "money.granted", credits=f"{amount:,}"))
        note = " They were told." if delivered else " Couldn't DM them."
    await send_markdown(bot, target, f"Granted {amount:,} dc to {kind} `{owner_id}`; balance {left:,}.{note}")


async def block(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    """``/block <user or chat id>`` / ``/unblock …``: Derp answers nothing there; commands still work."""
    on = (command.command or "").lower() == "block"
    target = Target.reply_to_message(message)
    raw = (command.args or "").strip()
    if not raw.lstrip("-").isdigit():
        await send_markdown(bot, target, f"Usage: `/{command.command} <user or chat id>`")
        return
    key = int(raw)
    async with db.transaction() as session:
        row = await session.get(Chat, key) if key < 0 else await session.get(User, key)
        if row is None:
            await send_markdown(bot, target, f"`{key}` isn't known.")
            return
        row.blocked_at = datetime.now(UTC) if on else None
    await send_markdown(bot, target, f"{'Blocked' if on else 'Unblocked'} `{key}`.")


def build_router() -> Router:
    router = Router(name="console")
    router.message.register(show_stats, Command("stats"), from_owner)
    router.message.register(grant, Command("grant"), from_owner)
    router.message.register(block, Command("block", "unblock"), from_owner)
    router.callback_query.register(
        on_console, ConsoleAction.filter(F.action.in_({"apply", "ignore", "rollback"}))
    )
    return router
