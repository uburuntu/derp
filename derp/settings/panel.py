"""/settings: one panel for the chat's policy and the asker's own choices.

Everyone sees the chat's settings; only admins (anyone, in a private chat)
change them, and the role is checked again on every press. Personal choices
(using my Derpcoins in groups) are each person's own. In groups /settings is
an ephemeral command, so the panel is ephemeral and its buttons edit it
through the ephemeral edit methods. When context is on but Telegram hides the
chat from Derp, the panel offers the make-me-admin link.

Destructive changes ask first: turning context off deletes the chat's stored
ambient history, and a shorter retention deletes older messages. The jobs page
lists the chat's reminders and scheduled prompts; people pause, resume or
delete their own, admins any (the Resume button on a paused job's note follows
the same rule). Callback data carries only ids, and every press re-checks that
the job belongs to this chat and who may touch it.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.members import admin_link, can_read_group
from derp.history.models import Chat, User
from derp.history.models import Message as StoredMessage
from derp.history.render import chat_time, one_line
from derp.locales import resolve, t
from derp.memory.notes import is_admin
from derp.money.free import POLICY_VERSION, consented
from derp.scheduler.jobs import ResumeJob, local_time, resume, rezone
from derp.scheduler.models import Job
from derp.settings.privacy import switch_context
from derp.telegram.buttons import Press, button, link, markup, on_press
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal
from derp.tools.timezones import TimeZoneError, iana

NOTES_MODES = ("auto", "members", "admins", "off")
RULES_PREVIEW = 300
JOB_ACTIONS = ("jpause", "jresume", "jdrop")
JOB_TEXT = 60  # characters of a job's text on the jobs page
JOBS_BUDGET = 3500  # characters of job lines before "…and N more"
LIVE = ("active", "paused")


class SettingsAction(CallbackData, prefix="set"):
    # page | ambient (on / purge) | retention | shorten (confirmed) | notes | free | rules | spend | named
    # | jpause / jresume / jdrop (value: the job id)
    action: str
    value: str = ""


def _act(action: str, value: str = "") -> SettingsAction:
    return SettingsAction(action=action, value=value)


def default_days(chat: Chat, settings: Settings) -> int:
    return settings.retention.private_days if chat.type == "private" else settings.retention.group_days


def kept_days(chat: Chat, settings: Settings) -> int:
    return chat.retention_days or default_days(chat, settings)


def _days(locale: str, days: int) -> str:
    return t(locale, "settings.day" if days == 1 else "settings.days", days=days)


def chosen_days(value: str, settings: Settings) -> int | None:
    """The retention a choice stands for: an offered number of days, 0 for the default; None if not offered."""
    days = int(value) if value.isdigit() else -1
    return days if days == 0 or days in settings.retention.options else None


def shortens(chat: Chat, settings: Settings, value: str) -> bool:
    days = chosen_days(value, settings)
    return days is not None and (days or default_days(chat, settings)) < kept_days(chat, settings)


async def _jobs_page(
    session: AsyncSession, chat: Chat, user_id: int | None, admin: bool, locale: str
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    rows = (
        await session.execute(
            select(Job, User.first_name)
            .outerjoin(User, User.id == Job.creator_id)
            .where(Job.chat_id == chat.id, Job.state.in_(LIVE))
            .order_by(Job.next_at)
        )
    ).all()
    tz = chat_time(chat)
    lines = [t(locale, "settings.jobs_page", zone=chat.timezone or "UTC")]
    if not rows:
        lines.append(t(locale, "settings.jobs_none"))
    buttons: list[list[InlineKeyboardButton]] = []
    used = 0
    for index, (job, name) in enumerate(rows):
        line = t(
            locale,
            "settings.job_line",
            kind="💬" if job.kind == "prompt" else "⏰",
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
        if admin or job.creator_id == user_id:
            paused = job.state == "paused"
            toggle = t(locale, "settings.job_resume" if paused else "settings.job_pause", id=job.id)
            buttons.append(
                [
                    button(toggle, _act("jresume" if paused else "jpause", str(job.id))),
                    button(t(locale, "settings.job_delete", id=job.id), _act("jdrop", str(job.id))),
                ]
            )
    if rows and chat.type != "private":
        lines.append(t(locale, "settings.jobs_own"))
    return "\n".join(lines), buttons


async def render(
    session: AsyncSession,
    settings: Settings,
    chat: Chat,
    user: User | None,
    locale: str,
    page: str = "main",
    *,
    can_read: bool = True,
    admin_url: str | None = None,
    admin: bool = False,
    viewer: int | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The panel page for this viewer; ``admin`` decides which jobs they get buttons for.

    ``admin_url`` (the make-me-admin link) is offered while context is on but Derp can't read the chat.
    """
    group = chat.type != "private"
    on, off = t(locale, "settings.on"), t(locale, "settings.off")
    back = button(t(locale, "settings.back"), _act("page", "main"))
    if page == "retention":
        choices = [button(_days(locale, d), _act("retention", str(d))) for d in settings.retention.options]
        default = _days(locale, default_days(chat, settings))
        reset = button(t(locale, "settings.default_days", days=default), _act("retention", "0"))
        return t(locale, "settings.retention_page"), markup(choices[:2], choices[2:], reset, back)
    if page.startswith("shorten:"):
        value = page.removeprefix("shorten:")
        days = _days(locale, chosen_days(value, settings) or default_days(chat, settings))
        label = t(locale, "settings.shorten_confirm", days=days)
        confirm = button(label, _act("shorten", value), style="danger")
        return t(locale, "settings.shorten_page", days=days), markup(confirm, back)
    if page == "notes":
        modes = [button(t(locale, f"settings.notes_{mode}"), _act("notes", mode)) for mode in NOTES_MODES]
        return t(locale, "settings.notes_page"), markup(*modes, back)
    if page == "ambient_off":
        ambient_count = await session.scalar(
            select(func.count()).where(StoredMessage.chat_id == chat.id, StoredMessage.capture == "ambient")
        )
        confirm = button(t(locale, "settings.context_off_confirm"), _act("ambient", "purge"), style="danger")
        return t(locale, "settings.context_off_page", count=ambient_count or 0), markup(confirm, back)
    if page == "jobs":
        text, rows = await _jobs_page(session, chat, viewer or (user.id if user else None), admin, locale)
        return text, markup(*rows, back)

    jobs = await session.scalar(select(func.count()).where(Job.chat_id == chat.id, Job.state.in_(LIVE)))
    lines = [
        t(locale, "settings.title", title=literal(one_line(chat.title or t(locale, "settings.this_chat"))))
    ]
    blind = group and chat.ambient and not can_read
    if group:
        context_line = (
            "settings.context_off"
            if not chat.ambient
            else "settings.context_needs_admin"
            if blind
            else "settings.context_on"
        )
        lines.append(t(locale, context_line))
    lines.append(t(locale, "settings.retention", days=_days(locale, kept_days(chat, settings))))
    if chat.timezone:
        lines.append(t(locale, "settings.zone", zone=chat.timezone))
    else:
        lines.append(t(locale, "settings.zone_unset"))
    lines.append(t(locale, "settings.notes", mode=t(locale, f"settings.notes_{chat.notes_mode}")))
    if chat.rules:
        lines.append(t(locale, "settings.rules", rules=literal(one_line(chat.rules, RULES_PREVIEW))))
    else:
        lines.append(t(locale, "settings.no_rules"))
    if group:
        lines.append(t(locale, "settings.free_group", state=on if chat.free_models else off))
    else:
        lines.append(t(locale, "settings.free_private", state=on if consented(user) else off))
    lines.append(t(locale, "settings.jobs", count=jobs or 0))
    spends = user is None or user.group_spending
    lines.append(t(locale, "settings.spend_on" if spends else "settings.spend_off"))
    named = user is None or user.patron_named
    lines.append(t(locale, "settings.named_on" if named else "settings.named_off"))
    if group:
        lines.append(t(locale, "settings.admins_only"))

    context = None
    if group:
        label = t(locale, "settings.context_button", state=on if chat.ambient else off)
        context = button(label, _act("page", "ambient_off") if chat.ambient else _act("ambient", "on"))
    kept = _days(locale, kept_days(chat, settings))
    mode = t(locale, f"settings.notes_{chat.notes_mode}")
    free_on = chat.free_models if group else consented(user)
    free = t(locale, "settings.free_button", state=on if free_on else off)
    spend = t(locale, "settings.spend_button", state=on if spends else off)
    keyboard = markup(
        link(t(locale, "onboarding.make_admin"), admin_url) if blind and admin_url else None,
        [context, button(t(locale, "settings.retention_button", days=kept), _act("page", "retention"))],
        button(t(locale, "settings.notes_button", mode=mode), _act("page", "notes")),
        button(free, _act("free", "off" if free_on else "on")),
        button(t(locale, "settings.rules_clear"), _act("rules", "clear")) if chat.rules else None,
        button(t(locale, "settings.jobs_button", count=jobs), _act("page", "jobs")) if jobs else None,
        button(spend, _act("spend", "off" if spends else "on")),
        button(
            t(locale, "settings.named_button", state=on if named else off),
            _act("named", "off" if named else "on"),
        ),
    )
    return "\n".join(lines), keyboard


async def _panel(
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
) -> tuple[str, InlineKeyboardMarkup | None]:
    """``render`` with what Telegram knows: whether Derp reads the chat, and the link that fixes it."""
    can_read = chat.type == "private" or await can_read_group(bot, chat)
    username = None if can_read else (await bot.me()).username
    return await render(
        session,
        settings,
        chat,
        user,
        locale,
        page,
        can_read=can_read,
        admin_url=admin_link(username) if username else None,
        admin=admin,
        viewer=viewer,
    )


async def on_settings(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    async with db.session() as session:
        chat = await session.get(Chat, message.chat.id)
        user = await session.get(User, message.from_user.id)
        if chat is None:
            return
        text, keyboard = await _panel(session, bot, settings, chat, user, locale)
    await send_markdown(bot, Target.reply_to_message(message), text, reply_markup=keyboard)


async def apply_setting(
    db: Database, settings: Settings, chat_id: int, user_id: int, action: str, value: str
) -> str | None:
    """Change one setting; returns the key of a notice to show, if any.

    The caller checked the role and asked first where it must: a shorter retention and
    context off are confirmed presses, and free-model consent (``free`` on in a private
    chat) only ever comes from the person's own press, never from a tool.
    """
    if action == "ambient" and value == "purge":
        await switch_context(db, chat_id, on=False)
        return "settings.context_purged"
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
                chat.free_models = value == "on"
            case "free" if person is not None:
                on = value == "on"
                person.free_models_consent_at = datetime.now(UTC) if on else None
                person.free_models_consent_version = POLICY_VERSION if on else None
            case "rules":
                chat.rules, chat.rules_draft, chat.rules_draft_by = None, None, None
            case "timezone":
                try:
                    name = iana(value)
                except TimeZoneError:
                    return None
                old, new = chat_time(chat), ZoneInfo(name)
                chat.timezone = name
                now = datetime.now(UTC)
                repeating = await session.scalars(
                    select(Job)
                    .where(Job.chat_id == chat.id, Job.state.in_(LIVE), Job.repeat.is_not(None))
                    .with_for_update()
                )
                for job in repeating:
                    rezone(job, old, new, now)
            case _:
                pass
    return None


async def apply_job(
    db: Database, chat: Chat, user_id: int, admin: bool, action: str, value: str
) -> str | None:
    """Pause, resume or delete one job of this chat (its creator or an admin); returns an alert key, if any."""
    if not value.isdigit():
        return "settings.job_gone"
    async with db.transaction() as session:
        job = await session.get(Job, int(value), with_for_update=True)
        if job is None or job.chat_id != chat.id or job.state not in LIVE:
            return "settings.job_gone"
        if job.creator_id != user_id and not admin:
            return "settings.job_not_yours"
        match action:
            case "jpause":
                job.state = "paused"
            case "jresume" if job.state == "paused":
                if not resume(job, datetime.now(UTC), chat_time(chat)):
                    return "settings.job_passed"
            case "jdrop":
                job.state = "cancelled"
            case _:
                pass
    return None


async def on_action(press: Press, data: SettingsAction, db: Database, bot: Bot, settings: Settings) -> None:
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None:
        return
    action, value = data.action, data.value
    jobs = action in JOB_ACTIONS or (action == "page" and value == "jobs")
    personal = jobs or action in {"spend", "named"} or (action == "page" and value == "main")
    admin = chat.type == "private" or ((jobs or not personal) and await is_admin(bot, chat, press.user_id))
    if not personal and not admin:
        return await press.deny("settings.admins_alert")
    if action == "page":
        page = value
    elif action in JOB_ACTIONS:
        page = "jobs"
        if alert := await apply_job(db, chat, press.user_id, admin, action, value):
            await press.deny(alert)  # and the page is redrawn as it is now
    elif action == "retention" and shortens(chat, settings, value):
        page = f"shorten:{value}"  # deletes older history: confirm first
    else:
        page = "main"
        await press.toast(await apply_setting(db, settings, chat.id, press.user_id, action, value))
    if action == "free" and value == "on" and chat.type != "private" and not chat.free_models:
        await send_markdown(bot, Target(chat.id), t(chat.locale or press.locale, "money.free_enabled"))
    async with db.session() as session:
        fresh = await session.get(Chat, chat.id)
        user = await session.get(User, press.user_id)
        if fresh is None:
            return
        text, keyboard = await _panel(
            session, bot, settings, fresh, user, press.locale, page, admin=admin, viewer=press.user_id
        )
    await press.edit(text, keyboard)


async def on_resume(press: Press, data: ResumeJob, db: Database, bot: Bot) -> None:
    """[▶️ Resume] on a paused job's note: its creator or an admin, checked on the press."""
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None:
        return await press.deny("settings.job_gone")
    admin = chat.type == "private" or await is_admin(bot, chat, press.user_id)
    alert = await apply_job(db, chat, press.user_id, admin, "jresume", str(data.id))
    if alert == "settings.job_not_yours":
        return await press.deny(alert)
    if alert:  # cancelled, finished or past its time: the button can't help any more
        await press.strip()
        return await press.deny(alert)
    await press.edit(t(chat.locale or press.locale, "scheduler.resumed", id=data.id))


def build_router() -> Router:
    router = Router(name="settings")
    router.message.register(on_settings, Command("settings"))
    actions = {
        "page",
        "ambient",
        "retention",
        "shorten",
        "notes",
        "free",
        "rules",
        "spend",
        "named",
        *JOB_ACTIONS,
    }
    on_press(router, SettingsAction, F.action.in_(actions))(on_action)
    on_press(router, ResumeJob, F.id > 0)(on_resume)
    return router
