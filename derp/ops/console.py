"""Owner console, unlisted: /stats, /grant, /block, /unblock in private, /context in any chat.

/stats shows provider spend (the provider-call ledger only; Derp's own fees are
counted apart), Stars revenue, runs, the busiest chats, the OpenRouter key's
remaining limit, the live model pins (with Roll back) and the model scout's
open proposals (Apply / Ignore). Anyone else's command falls through to the
unknown-command reply, so the console doesn't announce itself. Owner copy is
plain English, not localized.

/context [text] sends a web page of what the model would be sent if this
message (or one saying ``text``) invoked Derp here now (``ops.context``). It
only ever shows a chat an owner is in, and never to the chat: in groups it
answers ephemerally, and a typed-out /context@Derp there goes to the owner's
private chat.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import distinct, func, select

from derp.agent.models import AgentRun
from derp.agent.runtime import Runtime
from derp.billing.models import Purchase
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import chat_time, one_line
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.models import ModelPin, ProviderCall, ScoutProposal
from derp.inference.pins import PinStore, apply_pin, rollback_pin
from derp.inference.roles import ModelRole, RolePin
from derp.locales import resolve, t
from derp.money.models import CreditLot, RunFee
from derp.money.wallets import balance, credit, wallet
from derp.ops.context import render
from derp.ops.owner import from_owner, owner_here
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_document, send_markdown, send_privately
from derp.telegram.text import literal

WINDOWS = ((1, "24 h"), (7, "7 d"), (30, "30 d"))
FUNDINGS = ("paid", "sponsored", "free", "creator")
TOP_CHATS = 5
MAX_GRANT = 1_000_000

SCOUT_PING = "Model scout:"


class ConsoleAction(CallbackData, prefix="own"):
    action: str  # apply | ignore (a scout proposal) | rollback (a role's pin)
    key: str


def _usd(value: Decimal | float | None) -> str:
    return f"${float(value or 0):.2f}"


async def _key_line(api: OpenRouterAPI) -> str:
    try:
        key = await api.key_info()
    except OpenRouterError:
        return "unavailable"
    if key.get("limit_remaining") is None:
        return f"{_usd(key.get('usage'))} used, no limit"
    return f"{_usd(key['limit_remaining'])} left of {_usd(key.get('limit'))}"


def _table(header: list[str], align: str, rows: list[list[str]]) -> list[str]:
    """A Markdown table; ``align`` has one letter per column: l (left) or r (right)."""
    rule = "|" + "|".join(":--" if a == "l" else "--:" for a in align) + "|"
    return ["| " + " | ".join(header) + " |", rule, *("| " + " | ".join(row) + " |" for row in rows)]


async def stats(db: Database, api: OpenRouterAPI) -> tuple[str, InlineKeyboardMarkup | None]:
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

    lines = [
        "## 📊 Derp stats",
        *_table(
            ["", *(label for _, label in WINDOWS)],
            "lrrr",
            [
                ["Provider cost", *(_usd(c) for c in cost)],
                ["Revenue", *(f"{s or 0}⭐" for s in stars)],
                ["Refunds", *(str(r or 0) for r in refunds)],
                ["Runs", *(str(r or 0) for r in runs)],
            ],
        ),
        "",
        "### Last 7 days",
        "- **Cost by funding:** " + " · ".join(f"{f} {_usd(by_funding.get(f))}" for f in FUNDINGS),
        f"- **Derp fees:** {fees or 0} dc",
        f"- **Active:** {people} people in {chats} chats",
        f"- **Blocked:** {blocked or 0} people, {blocked_chats or 0} chats",
        f"- **OpenRouter key:** {await _key_line(api)}",
    ]
    if top:
        lines += ["", "### Top chats by cost (7 d)"]
        lines += _table(
            ["Chat", "ID", "Cost"],
            "llr",
            [[literal(one_line(title or "—")), f"`{chat_id}`", _usd(c)] for chat_id, title, c in top],
        )
    buttons: list[InlineKeyboardButton | list[InlineKeyboardButton]] = []
    if pins:
        lines += ["", "### Model pins"]
        lines += _table(
            ["Role", "Model", "Source"],
            "lll",
            [[pin.role, f"`{pin.pin.get('model')}`", pin.source] for pin in pins],
        )
        buttons += [
            button(
                f"Roll back {pin.role} → {pin.previous.get('model')}",
                ConsoleAction(action="rollback", key=pin.role),
            )
            for pin in pins
            if pin.previous
        ]
    if proposals:
        lines += ["", "### Scout proposals"]
        for p in proposals:
            lines += [
                f"**Proposal {p.id}** · {p.role}: `{p.current.get('model')}` → `{p.proposed.get('model')}`",
                literal(p.reason),
            ]
            buttons.append(_proposal_row(p.id))
    return "\n".join(lines), markup(*buttons)


def _proposal_row(proposal_id: int) -> list[InlineKeyboardButton]:
    return [
        button(f"Apply #{proposal_id}", ConsoleAction(action="apply", key=str(proposal_id))),
        button(f"Ignore #{proposal_id}", ConsoleAction(action="ignore", key=str(proposal_id))),
    ]


def proposal_buttons(proposal_ids: list[int]) -> InlineKeyboardMarkup | None:
    """Apply / Ignore for each proposal, attached to the scout's ping (the same actions as in /stats)."""
    return markup(*(_proposal_row(pid) for pid in proposal_ids))


async def show_stats(message: Message, db: Database, api: OpenRouterAPI, bot: Bot) -> None:
    text, keyboard = await stats(db, api)
    await send_markdown(bot, Target.reply_to_message(message), text, reply_markup=keyboard)


async def decide(action: str, key: str, db: Database, pins: PinStore) -> tuple[str, bool]:
    """Apply, ignore or roll back; a note for the owner and whether anything changed."""
    now = datetime.now(UTC)
    async with db.transaction() as session:
        if action == "rollback":
            if key not in {role.value for role in ModelRole} or not await rollback_pin(
                session, ModelRole(key)
            ):
                return "Nothing to roll back.", False
            note = f"Rolled back {key}."
        else:
            proposal = await session.get(
                ScoutProposal, int(key) if key.isdigit() else 0, with_for_update=True
            )
            if proposal is None or proposal.status != "open":
                return "Already decided.", False
            if action == "apply":
                pin = RolePin.model_validate(proposal.proposed)
                await apply_pin(session, ModelRole(proposal.role), pin, source="owner")
            proposal.status = "applied" if action == "apply" else "ignored"
            proposal.decided_at = now
            note = f"Proposal #{proposal.id} {proposal.status}."
    await pins.refresh()
    return note, True


async def on_console(
    press: Press, data: ConsoleAction, db: Database, api: OpenRouterAPI, pins: PinStore, settings: Settings
) -> None:
    if press.user_id not in settings.owner_ids:
        return  # the console doesn't announce itself: a silent answer
    note, done = await decide(data.action, data.key, db, pins)
    await press.answer(note, alert=not done)
    if (press.message.text or "").startswith(SCOUT_PING):  # a ping: drop the decided proposal's buttons
        decided = {b.callback_data for b in _proposal_row(int(data.key))} if data.key.isdigit() else set()
        return await press.strip(keep=lambda b: b.callback_data not in decided)
    text, keyboard = await stats(db, api)
    await press.edit(text, keyboard)


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
    operation_id = f"grant:{message.chat.id}:{message.message_id}"
    known, fresh, left, language = False, False, 0, None
    async with db.transaction() as session:  # Telegram calls wait until it's committed
        recipient = await session.get(Chat if kind == "chat" else User, owner_id)
        if recipient is not None:
            known = True
            account = await wallet(session, kind, owner_id)
            account.empty_noticed_at = None  # a chat wallet topped up may be announced as empty again
            wallet_id = account.id
            fresh = (
                await session.scalar(select(CreditLot.id).where(CreditLot.operation_id == operation_id))
                is None
            )
            await credit(session, wallet_id, operation_id, amount, "grant")
            left = await balance(session, wallet_id)
            language = recipient.language_code if isinstance(recipient, User) else None
    if not known:
        await send_markdown(bot, target, f"No {kind} `{owner_id}` known.")
        return
    if not fresh:  # a redelivered command: credited once, told once
        return
    note = ""
    if kind == "user":
        text = t(resolve(language), "money.granted", credits=f"{amount:,}")
        note = " They were told." if await send_privately(bot, owner_id, text) else " Couldn't DM them."
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
        if row is not None:
            row.blocked_at = datetime.now(UTC) if on else None
    if row is None:
        await send_markdown(bot, target, f"`{key}` isn't known.")
        return
    await send_markdown(bot, target, f"{'Blocked' if on else 'Unblocked'} `{key}`.")


async def show_context(
    message: Message, command: CommandObject, bot: Bot, runtime: Runtime, chat_row: Chat
) -> None:
    if message.from_user is None:
        return
    asked = message.model_copy(update={"text": command.args, "entities": None}) if command.args else message
    target = Target.reply_to_message(message)
    if message.chat.type != "private" and not message.ephemeral_message_id:
        target = Target(message.from_user.id)  # the page holds the chat's context: never posted there
    preview = await runtime.preview(asked, chat_row)
    if preview is None:
        await send_markdown(
            bot, target, "Nobody can pay for a turn here: a message would get the Derpcoins offer."
        )
        return
    now = datetime.now(chat_time(chat_row))
    title = one_line(chat_row.title or message.from_user.full_name)
    page = render(preview, chat=title, asker=message.from_user.full_name, when=now)
    name = f"context-{chat_row.id}-{now:%Y%m%d-%H%M}.html"
    caption = f"{title} · {preview.funding.value} · {preview.pin.model}"
    await send_document(bot, target, BufferedInputFile(page.encode(), filename=name), caption=caption)


def build_router() -> Router:
    router = Router(name="console")
    router.message.register(show_context, Command("context"), owner_here)
    router.message.register(show_stats, Command("stats"), from_owner)
    router.message.register(grant, Command("grant"), from_owner)
    router.message.register(block, Command("block", "unblock"), from_owner)
    on_press(router, ConsoleAction, F.action.in_({"apply", "ignore", "rollback"}))(on_console)
    return router
