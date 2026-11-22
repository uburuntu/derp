"""History and behavior pages of the settings menu."""

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.history.members import context_state
from derp.history.models import Chat
from derp.history.retention import kept_days
from derp.locales import day_count, t
from derp.memory.models import ReplyRule
from derp.settings.navigation import act, footer, page_button
from derp.telegram.buttons import button, link, markup
from derp.telegram.text import truncate


async def rule_counts(session: AsyncSession, chat_id: int) -> tuple[int, int]:
    """Enabled and paused automatic replies in one chat. Drafts appear only on their approval cards."""
    counts = await session.execute(
        select(
            func.count().filter(ReplyRule.state == "active"),
            func.count().filter(ReplyRule.state == "paused"),
        ).where(ReplyRule.chat_id == chat_id)
    )
    active, paused = counts.one()
    return active, paused


async def history(
    session: AsyncSession, bot: Bot, settings: Settings, chat: Chat, locale: str, *, admin: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    group = chat.type != "private"
    context = await context_state(bot, chat) if group else None
    access = context.state if context else "private"
    mode = "private_admins" if not group and chat.notes_mode == "admins" else chat.notes_mode
    text = "\n\n".join(
        [
            f"**{t(locale, 'navigation.history')}**",
            t(locale, f"sections.access_{access}")
            + "\n"
            + t(locale, "sections.retention", days=day_count(locale, kept_days(chat, settings.retention))),
            t(locale, "sections.notes", policy=t(locale, f"sections.notes_{mode}")),
        ]
    )
    context_button = None
    if admin and group:
        context_button = (
            page_button(locale, "ambient_off", label=t(locale, "sections.context_disable"))
            if chat.ambient
            else button(t(locale, "sections.context_enable"), act("ambient", "on"))
        )
    return text, markup(
        link(t(locale, "sections.make_admin"), context.admin_url)
        if admin and context and context.state == "needs_admin" and context.admin_url
        else None,
        context_button,
        page_button(locale, "retention", label=t(locale, "sections.change_retention")) if admin else None,
        page_button(locale, "notes", label=t(locale, "sections.change_notes")) if admin else None,
        footer(locale),
    )


async def behavior(
    session: AsyncSession, bot: Bot, chat: Chat, locale: str, *, admin: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Plain copy for the HTML card renderer: standing instructions are always literal."""
    group = chat.type != "private"
    rules = (chat.rules or "").strip()
    preview = truncate(rules, 650)
    standing = (
        t(locale, "sections.instructions") + "\n" + (preview or t(locale, "sections.instructions_empty"))
    )
    if admin:
        standing += "\n" + t(locale, "sections.instructions_hint")
    lines = [t(locale, "navigation.behavior"), standing]
    if group:
        active, paused = await rule_counts(session, chat.id)
        triggers = [t(locale, "sections.triggers")]
        if active or paused:
            triggers.append(t(locale, "sections.trigger_counts", active=active, paused=paused))
            if active:
                context = await context_state(bot, chat)
                triggers.append(t(locale, f"sections.triggers_context_{context.state}"))
        else:
            triggers.append(t(locale, "sections.triggers_empty"))
        if admin:
            triggers.append(t(locale, "sections.triggers_hint"))
        lines.append("\n".join(triggers))
    return "\n\n".join(lines), markup(
        page_button(locale, "instructions", label=t(locale, "sections.read_instructions"))
        if preview != rules
        else None,
        page_button(locale, "auto", label=t(locale, "sections.manage_triggers")) if admin and group else None,
        page_button(locale, "clear_rules", label=t(locale, "sections.clear_instructions"))
        if admin and rules
        else None,
        footer(locale),
    )


def instructions(chat: Chat, locale: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Full, read-only standing instructions, with the same literal rendering as the preview."""
    rules = (chat.rules or "").strip()
    text = t(locale, "sections.instructions") + "\n\n" + (rules or t(locale, "sections.instructions_empty"))
    return text, markup(footer(locale, "behavior"))
