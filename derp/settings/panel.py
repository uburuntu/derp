"""/settings navigation, permission checks and changes to chat or personal policy."""

import json
from contextlib import suppress
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.models import Message as StoredMessage
from derp.history.render import chat_time, one_line
from derp.history.retention import default_days, kept_days
from derp.keys.flows import chat_keys, chat_keys_page
from derp.locales import day_count, resolve, t
from derp.memory import reply_rules
from derp.memory.command import rules_digest
from derp.memory.notes import is_admin
from derp.money.free import POLICY_VERSION
from derp.scheduler.jobs import (
    SNOOZES,
    JobAction,
    ResumeJob,
    Snooze,
    SnoozeJob,
    act_on_job,
    live_jobs,
    local_time,
    may_touch,
    rezone,
    snoozed_until,
)
from derp.scheduler.models import Job
from derp.settings.navigation import SettingsAction, act, footer
from derp.settings.overview import funding, overview, personal
from derp.settings.privacy import switch_context
from derp.settings.sections import behavior, history, instructions
from derp.telegram.buttons import Press, button, dismiss_button, markup, on_press
from derp.telegram.filters import Command
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal
from derp.tools.timezones import TimeZoneError, iana

NOTES_MODES = ("auto", "members", "admins", "off")
JOB_ACTIONS: dict[str, JobAction] = {"jpause": "pause", "jresume": "resume", "jdrop": "cancel"}
JOB_TEXT = 60  # characters of a job's text on the jobs page
JOBS_BUDGET = 3500  # characters of job lines before "…and N more"


def chosen_days(value: str, settings: Settings) -> int | None:
    """The retention a choice stands for: an offered number of days, 0 for the default; None if not offered."""
    days = int(value) if value.isdigit() else -1
    return days if days == 0 or days in settings.retention.options else None


def shortens(chat: Chat, settings: Settings, value: str) -> bool:
    days = chosen_days(value, settings)
    retention = settings.retention
    return days is not None and (days or default_days(chat, retention)) < kept_days(chat, retention)


async def _jobs_page(
    session: AsyncSession, chat: Chat, user_id: int | None, admin: bool, locale: str
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    rows = await live_jobs(session, chat.id)
    tz = chat_time(chat)
    lines = [
        t(locale, "settings.jobs_page"),
        t(locale, "settings.zone", zone=chat.timezone) if chat.timezone else t(locale, "settings.zone_unset"),
    ]
    if not rows:
        lines.append(t(locale, "settings.jobs_none"))
    buttons: list[list[InlineKeyboardButton]] = []
    used = 0
    for index, (job, name) in enumerate(rows):
        line = t(
            locale,
            "settings.job_line",
            kind=t(locale, "settings.job_prompt" if job.kind == "prompt" else "settings.job_reminder"),
            id=job.id,
            when=local_time(job.next_at, tz),
            repeat=t(locale, f"settings.repeat_{job.repeat or 'once'}"),
            paused=t(locale, "settings.job_paused") if job.state == "paused" else "",
            name=literal(one_line(name or "?", 32)),
            text=literal(one_line(job.text, JOB_TEXT)),
        )
        if used + len(line) > JOBS_BUDGET:
            lines.append(t(locale, "settings.jobs_more", count=len(rows) - index))
            break
        used += len(line)
        lines.append(line)
        if may_touch(job, user_id, admin):
            paused = job.state == "paused"
            toggle = t(locale, "settings.job_resume" if paused else "settings.job_pause", id=job.id)
            buttons.append(
                [
                    button(toggle, act("jresume" if paused else "jpause", str(job.id))),
                    button(t(locale, "settings.job_delete", id=job.id), act("jdrop", str(job.id))),
                ]
            )
    if rows and chat.type != "private":
        lines.append(t(locale, "settings.jobs_own"))
    return "\n\n".join(lines), buttons


async def render(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    chat: Chat,
    user: User | None,
    locale: str,
    page: str = "main",
    *,
    admin: bool = False,
    viewer: int | None = None,
    standalone: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The panel page for this viewer; ``admin`` decides which jobs they get buttons for.

    ``user`` is the viewer whose own choices the page shows: None on a panel the whole group sees.
    """
    if page == "main":
        return await overview(session, bot, settings, chat, user, locale)
    if page == "history":
        return await history(session, bot, settings, chat, locale, admin=admin)
    if page == "behavior":
        return await behavior(session, bot, chat, locale, admin=admin)
    if page == "instructions":
        return instructions(chat, locale)
    if page == "funding":
        return await funding(session, bot, settings, chat, user, locale, admin=admin)
    if page == "personal" and user is not None:
        return personal(user, locale)
    if page == "jobs":
        text, rows = await _jobs_page(session, chat, viewer, admin, locale)
        return text, markup(*rows, footer(locale))
    if page == "keys":
        keys = await chat_keys(session, chat.id)
        text, rows = chat_keys_page(settings, keys, viewer, admin, locale)
        return text, markup(*rows, footer(locale, "funding"))
    if page == "auto":
        text, rows = await reply_rules.page(session, chat, locale)
        return text, markup(*rows)
    if page == "clear_rules":
        confirm = button(
            t(locale, "navigation.clear_confirm"), act("rules", _clear_version(chat)), style="danger"
        )
        parts = [t(locale, "navigation.clear_prompt"), chat.rules or t(locale, "sections.instructions_empty")]
        if chat.rules_draft is not None:
            parts.append(t(locale, "navigation.clear_draft"))
        return "\n\n".join(parts), markup(confirm, footer(locale, "behavior"))

    retention = settings.retention
    kept = day_count(locale, kept_days(chat, retention))
    if page == "retention":
        choices = [button(day_count(locale, d), act("retention", str(d))) for d in retention.options]
        default = day_count(locale, default_days(chat, retention))
        reset = button(t(locale, "settings.default_days", days=default), act("retention", "0"))
        return t(locale, "settings.retention_page", current=kept), markup(
            choices[:2], choices[2:], reset, footer(locale, "history")
        )
    if page == "notes":
        modes = [button(t(locale, f"settings.notes_{mode}"), act("notes", mode)) for mode in NOTES_MODES]
        current = t(locale, f"settings.notes_{chat.notes_mode}")
        return t(locale, "settings.notes_page", current=current), markup(*modes, footer(locale, "history"))

    cancel = button(t(locale, "settings.cancel"), act("finish", "cancel")) if standalone else None
    if page.startswith("shorten:"):
        value = page.removeprefix("shorten:")
        days = day_count(locale, chosen_days(value, settings) or default_days(chat, retention))
        confirm = button(
            t(locale, "settings.shorten_confirm", days=days), act("shorten", value), style="danger"
        )
        return t(locale, "settings.shorten_page", current=kept, days=days), markup(
            confirm, cancel or footer(locale, "retention")
        )
    if page == "ambient_off":
        count = await session.scalar(
            select(func.count()).where(StoredMessage.chat_id == chat.id, StoredMessage.capture == "ambient")
        )
        confirm = button(t(locale, "settings.context_off_confirm"), act("ambient", "purge"), style="danger")
        return t(locale, "settings.context_off_page", count=count or 0), markup(
            confirm, cancel or footer(locale, "history")
        )
    raise ValueError(f"Unknown settings page: {page}")


def _clear_version(chat: Chat) -> str:
    reviewed = json.dumps([chat.rules, chat.rules_draft, chat.rules_draft_by])
    return f"clear-{rules_digest(reviewed)}"


async def on_settings(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    async with db.session() as session:
        chat = await session.get(Chat, message.chat.id)
        user = await session.get(User, message.from_user.id)
        if chat is None:
            return
        own = chat.type == "private" or bool(message.ephemeral_message_id)  # else the whole group sees it
        if not own:
            locale = chat.locale or locale
        text, keyboard = await render(session, bot, settings, chat, user if own else None, locale)
    await send_markdown(bot, Target.reply_to_message(message), text, reply_markup=keyboard)


async def apply_setting(
    db: Database, settings: Settings, chat_id: int, user_id: int, action: str, value: str
) -> str | None:
    """Change one setting; returns the key of a notice to show, if any.

    The caller checked the role and asked first where it must: a shorter retention and
    context off are confirmed presses, and free-model consent (``free`` on in a private
    chat) only ever comes from the person's own press, never from a tool. Personal
    choices (``spend``, ``named``) are written here only, whoever asks.
    """
    if action == "ambient" and value == "purge":
        await switch_context(db, chat_id, on=False)
        return "settings.context_purged"
    shortened = action == "shorten" and chosen_days(value, settings) is not None
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id, with_for_update=True)
        person = await session.get(User, user_id)
        if chat is None:
            return None
        match action:
            case "spend" if person is not None:
                person.group_spending = value == "on"
            case "named" if person is not None:
                person.patron_named = value == "on"
            case "ambient":
                chat.ambient = True
            case "retention" | "shorten":
                days = chosen_days(value, settings)
                if days is not None:
                    chat.retention_days = days or None
            case "notes" if value in NOTES_MODES:
                chat.notes_mode = value
            case "free" if chat.type != "private":
                announce = value == "on" and not chat.free_models
                chat.free_models = value == "on"
                return "money.free_enabled" if announce else None
            case "free" if person is not None:
                on = value == "on"
                person.free_models_consent_at = datetime.now(UTC) if on else None
                person.free_models_consent_version = POLICY_VERSION if on else None
            case "rules":
                if value != _clear_version(chat):
                    return "navigation.instructions_changed"
                chat.rules, chat.rules_draft, chat.rules_draft_by = None, None, None
                return "navigation.cleared"
            case "timezone":
                try:
                    await rezone(session, chat, iana(value), datetime.now(UTC))
                except TimeZoneError:
                    return None
            case _:
                pass
    return "settings.shortened" if shortened else None


async def apply_job(
    db: Database, chat: Chat, user_id: int, admin: bool, action: JobAction | Snooze, value: str
) -> str | None:
    """A press on one of this chat's jobs (``scheduler.jobs.act_on_job`` decides); returns an alert key, if any."""
    if not value.isdigit():
        return "settings.job_gone"
    async with db.transaction() as session:
        outcome = await act_on_job(session, chat, int(value), user_id, admin, action)
    return f"settings.job_{outcome}" if isinstance(outcome, str) else None


def _ends_confirmation(data: SettingsAction) -> bool:
    """The press is a confirmation page's confirm button or its way out."""
    return data.action in ("shorten", "cancel") or (data.action, data.value) == ("ambient", "purge")


async def on_action(press: Press, data: SettingsAction, db: Database, bot: Bot, settings: Settings) -> None:
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None:
        return
    action, value = data.action, data.value
    private = chat.type == "private"
    own = private or press.ephemeral
    if (private and chat.id != press.user_id) or (
        press.ephemeral
        and (not press.message.receiver_user or press.message.receiver_user.id != press.user_id)
    ):
        return await press.deny("navigation.private_only")
    if not own and (action in {"named", "spend"} or (action, value) == ("page", "personal")):
        return await press.deny("navigation.private_only")
    visible = {"main", "history", "behavior", "instructions", "funding", "personal", "jobs", "keys"}
    if action == "page" and (
        value not in visible | {"clear_rules", "retention", "notes", "ambient_off", "auto"}
        or (private and value in {"keys", "auto", "ambient_off"})
    ):
        return await press.deny("buttons.stale")
    # Even a shared panel last opened by an admin must check the person pressing it now.
    admin = await is_admin(bot, chat, press.user_id)
    if not (admin or action in {"spend", "named", *JOB_ACTIONS} or (action == "page" and value in visible)):
        return await press.deny("settings.admins_alert")
    if not press.has_pressed_button():
        return await press.deny("settings.job_gone" if action == "jdrop" else "buttons.stale")
    locale = press.locale if own else chat.locale or press.locale
    callbacks = (
        {b.callback_data for row in press.message.reply_markup.inline_keyboard for b in row}
        if press.message.reply_markup
        else set()
    )
    # Old public tool prompts used the panel's Cancel, new ones have a distinct Finish.
    standalone = action == "finish" or (
        _ends_confirmation(data)
        and (act("finish", "cancel").pack() in callbacks or (not own and act("cancel").pack() in callbacks))
    )
    if action == "finish":
        if value != "cancel":
            return await press.deny("buttons.stale")
        action = "cancel"
    destructive = (
        action == "shorten"
        or (action, value) == ("ambient", "purge")
        or (action == "rules" and value.startswith("clear-"))
    )
    if (standalone or destructive) and not await press.take_button(clear=True, allow_group=True):
        return await press.deny("buttons.stale")
    if action == "page":
        page = value
    elif (action, value) == ("rules", "clear"):
        page = "clear_rules"  # Older panels have a direct Clear button. Show the review first.
    elif action in JOB_ACTIONS:
        page = "jobs"
        if alert := await apply_job(db, chat, press.user_id, admin, JOB_ACTIONS[action], value):
            if alert == "settings.job_gone":
                return await press.deny(alert)  # a repeated cancellation preserves its receipt
            await press.deny(alert)  # refresh the management page with their current permissions
        else:
            controls = (
                None
                if action == "jdrop"
                else markup(
                    [
                        dismiss_button(locale, user_id=press.user_id),
                        button(t(locale, "settings.manage_jobs"), act("page", "jobs")),
                    ]
                )
            )
            return await press.edit(t(locale, f"settings.done_{action}", id=value), controls)
    elif action == "retention" and shortens(chat, settings, value):
        page = f"shorten:{value}"  # deletes older history: confirm first
    else:
        page = {
            "retention": "history",
            "notes": "history",
            "ambient": "history",
            "shorten": "history",
            "rules": "behavior",
            "free": "funding",
            "spend": "personal",
            "named": "personal",
        }.get(action, "main")
        notice = (
            await apply_setting(db, settings, chat.id, press.user_id, action, value)
            if action != "cancel"
            else "settings.cancelled"
        )
        if standalone or destructive:
            keyboard = None if standalone else markup(footer(locale, page))
            return await press.edit(t(locale, notice or "settings.invalid"), keyboard)
        if notice == "money.free_enabled":
            await send_markdown(bot, Target(chat.id), t(chat.locale or locale, notice))
        elif action != "cancel":
            await press.toast(notice)
    async with db.session() as session:
        fresh = await session.get(Chat, chat.id)
        user = await session.get(User, press.user_id) if own else None
        if fresh is None or (page == "personal" and user is None):
            return await press.deny("buttons.stale")
        text, keyboard = await render(
            session, bot, settings, fresh, user, locale, page, admin=admin, viewer=press.user_id
        )
    await press.edit(
        text,
        keyboard,
        render=reply_rules.card_html if page in {"auto", "behavior", "instructions", "clear_rules"} else None,
    )


async def _claim_job_button(press: Press, db: Database, chat: Chat, job_id: int, admin: bool) -> bool:
    """Only someone who may act on this job can consume its reminder controls."""
    async with db.session() as session:
        job = await session.get(Job, job_id)
    if job is None or job.chat_id != chat.id:
        await press.deny("settings.job_gone")
    elif not may_touch(job, press.user_id, admin):
        await press.deny("settings.job_not_yours")
    elif await press.take_button(clear=True, allow_group=True):
        return True
    else:
        await press.deny("buttons.stale")
    return False


async def on_resume(press: Press, data: ResumeJob, db: Database, bot: Bot) -> None:
    """[▶️ Resume] on a paused job's note: its creator or an admin, checked on the press."""
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None:
        return await press.deny("settings.job_gone")
    admin = await is_admin(bot, chat, press.user_id)
    if not await _claim_job_button(press, db, chat, data.id, admin):
        return
    alert = await apply_job(db, chat, press.user_id, admin, "resume", str(data.id))
    if alert:
        return await press.deny(alert)
    await press.edit(t(chat.locale or press.locale, "scheduler.resumed", id=data.id))


async def on_snooze(press: Press, data: SnoozeJob, db: Database, bot: Bot) -> None:
    """[+10 min] [+1 h] [Tomorrow] on a fired reminder: its creator or an admin, checked on the press."""
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None or data.by not in SNOOZES:
        return await press.deny("settings.job_gone")
    admin = await is_admin(bot, chat, press.user_id)
    if not await _claim_job_button(press, db, chat, data.id, admin):
        return
    snooze = Snooze(snoozed_until(data.by, datetime.now(UTC), chat_time(chat)), data.run)
    alert = await apply_job(db, chat, press.user_id, admin, snooze, str(data.id))
    if alert == "settings.job_full":
        # No snooze was recorded. The creator can free a slot and retry the same firing.
        async with db.session() as session:
            job = await session.get(Job, data.id)
        if job and job.chat_id == chat.id and job.state == "active" and job.runs == data.run:
            with suppress(TelegramAPIError):
                await press.set_keyboard(press.message.reply_markup)
    if alert:
        return await press.deny(alert)
    await press.toast(f"scheduler.snoozed_{data.by}")


def build_router() -> Router:
    router = Router(name="settings")
    router.message.register(on_settings, Command("settings"))
    actions = {
        "page",
        "ambient",
        "retention",
        "shorten",
        "finish",
        "cancel",
        "notes",
        "free",
        "rules",
        "spend",
        "named",
        *JOB_ACTIONS,
    }
    on_press(router, SettingsAction, F.action.in_(actions))(on_action)
    on_press(router, ResumeJob, F.id > 0)(on_resume)
    on_press(router, SnoozeJob, F.id > 0)(on_snooze)
    return router
