"""The /settings overview, funding in this chat, and the viewer's own preferences."""

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.history.members import context_state
from derp.history.models import Chat, User
from derp.history.render import chat_time, one_line
from derp.history.retention import kept_days
from derp.keys.flows import plug_link
from derp.keys.models import ContributedKey, KeyChat
from derp.keys.notices import KeyAction
from derp.locales import day_count, t
from derp.money.amounts import format_dc
from derp.money.free import consented
from derp.money.funding import PAID_SOURCES
from derp.money.models import Wallet
from derp.money.wallets import balance
from derp.scheduler.models import Job
from derp.settings.navigation import act, footer, page_button
from derp.settings.sections import rule_counts
from derp.telegram.buttons import button, dismiss_button, link, markup
from derp.telegram.text import literal


async def _wallet_line(
    session: AsyncSession, settings: Settings, chat: Chat, user: User | None, locale: str
) -> str:
    """Spendable coins only. Reading settings never opens a wallet or grants an allowance."""
    private = chat.type == "private"
    if private and user is None:
        return ""
    now = datetime.now(UTC)
    wallet_id = await session.scalar(
        select(Wallet.id).where(
            Wallet.kind == ("user" if private else "chat"),
            Wallet.owner_id == (user.id if private and user is not None else chat.id),
        )
    )
    sources = PAID_SOURCES if private else None
    paid = await balance(session, wallet_id, now=now, sources=sources) if wallet_id is not None else 0
    if private and user is not None:
        from derp.money.command import free_left_today  # the /credits handler also imports settings

        free = await free_left_today(session, settings, user.id, now)
        return t(locale, "overview.wallet_personal", paid=format_dc(paid), free=format_dc(free))
    return t(locale, "overview.wallet_group", paid=format_dc(paid))


async def overview(
    session: AsyncSession, bot: Bot, settings: Settings, chat: Chat, user: User | None, locale: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    group = chat.type != "private"
    days = day_count(locale, kept_days(chat, settings.retention))
    if group:
        name = literal(one_line(chat.title or t(locale, "settings.this_chat")))
        title = t(locale, "overview.group_title", title=name)
        context = await context_state(bot, chat)
        enabled, _paused = await rule_counts(session, chat.id)
        reply = "overview.called"
        if enabled:
            reply = "overview.rules" if context.state == "on" else "overview.rules_limited"
        lines = [t(locale, f"overview.history_{context.state}", days=days), t(locale, reply, count=enabled)]
    else:
        title = t(locale, "overview.private_title")
        lines = [t(locale, "overview.history_private", days=days)]
    next_at = await session.scalar(
        select(func.min(Job.next_at)).where(Job.chat_id == chat.id, Job.state == "active")
    )
    if next_at is not None:
        zone = chat_time(chat)
        local = next_at.astimezone(zone)
        days_away = (local.date() - datetime.now(zone).date()).days
        day = {0: "today", 1: "tomorrow"}.get(days_away, "date")
        when = t(locale, f"overview.job_{day}", at=local)
        lines.append(t(locale, "overview.next_job", when=when, zone=zone))
    lines.append(await _wallet_line(session, settings, chat, user, locale))
    if not group and user is not None:
        lines.append(
            t(locale, "overview.group_spend_on" if user.group_spending else "overview.group_spend_off")
        )
    text = title + "\n\n" + "\n".join(line for line in lines if line)
    return text, markup(
        [page_button(locale, "history"), page_button(locale, "behavior")],
        [page_button(locale, "funding"), page_button(locale, "jobs")],
        [page_button(locale, "personal") if user is not None else None, dismiss_button(locale)],
    )


async def funding(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    chat: Chat,
    user: User | None,
    locale: str,
    *,
    admin: bool,
) -> tuple[str, InlineKeyboardMarkup | None]:
    group = chat.type != "private"
    free_on = chat.free_models if group else consented(user)
    lines = [
        f"**{t(locale, 'navigation.funding')}**",
        await _wallet_line(session, settings, chat, user, locale),
        t(locale, "overview.order_group" if group else "overview.order_private"),
    ]
    key_count = 0
    if group:
        key_count = await session.scalar(
            select(func.count())
            .select_from(KeyChat)
            .join(ContributedKey)
            .where(KeyChat.chat_id == chat.id, KeyChat.state == "on", ContributedKey.removed_at.is_(None))
        )
        lines.append(t(locale, "overview.keys_group", count=key_count or 0))
        keys = [
            page_button(locale, "keys", label=t(locale, "overview.manage_keys")),
            link(t(locale, "overview.plug_key"), await plug_link(bot, settings, chat.id)),
        ]
    elif user is not None:
        key_count = await session.scalar(
            select(func.count())
            .select_from(ContributedKey)
            .where(ContributedKey.user_id == user.id, ContributedKey.removed_at.is_(None))
        )
        lines.append(t(locale, "overview.keys_private", count=key_count or 0))
        keys = [
            button(t(locale, "overview.my_keys"), KeyAction(action="page", key=0)) if key_count else None,
            button(t(locale, "overview.plug_key"), KeyAction(action="plug", key=0)),
        ]
    else:
        keys = []
    if key_count:
        lines[-1] += " " + t(locale, "overview.key_privacy")
    if group or user is not None:
        state = t(locale, "settings.on" if free_on else "settings.off")
        lines.append(t(locale, "overview.free_group" if group else "overview.free_private", state=state))
    free = button(
        t(locale, "overview.free_disable" if free_on else "overview.free_allow"),
        act("free", "off" if free_on else "on"),
    )
    may_change = admin if group else user is not None
    return "\n\n".join(line for line in lines if line), markup(
        keys, free if may_change else None, footer(locale)
    )


def personal(user: User, locale: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Only the recipient of a private or ephemeral panel can see or change these preferences."""
    spends, named = user.group_spending, user.patron_named
    lines = [
        f"**{t(locale, 'navigation.personal')}**",
        t(locale, "overview.spend_on" if spends else "overview.spend_off"),
        t(locale, "overview.named_on" if named else "overview.named_off"),
    ]
    return "\n\n".join(lines), markup(
        button(
            t(locale, "overview.spend_disable" if spends else "overview.spend_allow"),
            act("spend", "off" if spends else "on"),
        ),
        button(
            t(locale, "overview.named_hide" if named else "overview.named_show"),
            act("named", "off" if named else "on"),
        ),
        footer(locale),
    )
