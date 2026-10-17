"""/settings: one panel for the chat's policy and the asker's own choices.

Everyone sees the chat's settings; only admins (anyone, in a private chat)
change them, and the role is checked again on every press. Personal choices
(using my Derpcoins in groups, being named when chipping in) are each
person's own and show only on a panel nobody else sees. In groups /settings
is an ephemeral command, so the panel is ephemeral and its buttons edit it
through the ephemeral edit methods; one typed out in full is public and
leaves them out. While Telegram hides the chat from Derp,
the panel offers the make-me-admin link.

Destructive changes ask first: turning context off deletes the chat's stored
ambient history, and a shorter retention deletes older messages. A confirmation
page Derp posted into a group on someone's request isn't anyone's panel: a press
there toasts the outcome and drops the buttons instead of opening a panel. The
jobs page lists the chat's reminders and scheduled prompts with buttons for the
ones the viewer may touch; ``scheduler.jobs`` decides, here and for the Resume
button on a paused job's note. Callback data carries only ids.
"""

from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.members import context_state
from derp.history.models import Chat, User
from derp.history.models import Message as StoredMessage
from derp.history.render import chat_time, one_line
from derp.history.retention import default_days, kept_days
from derp.locales import day_count, resolve, t
from derp.memory.notes import is_admin
from derp.money.free import POLICY_VERSION, consented
from derp.scheduler.jobs import JobAction, ResumeJob, act_on_job, live_jobs, local_time, may_touch, rezone
from derp.settings.privacy import switch_context
from derp.telegram.buttons import Press, button, link, markup, on_press
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal
from derp.tools.timezones import TimeZoneError, iana

NOTES_MODES = ("auto", "members", "admins", "off")
RULES_PREVIEW = 300
JOB_ACTIONS: dict[str, JobAction] = {"jpause": "pause", "jresume": "resume", "jdrop": "cancel"}
JOB_TEXT = 60  # characters of a job's text on the jobs page
JOBS_BUDGET = 3500  # characters of job lines before "…and N more"


class SettingsAction(CallbackData, prefix="set"):
    # page | ambient (on / purge) | retention | shorten (confirmed) | cancel (a confirmation) | notes | free
    # | rules | spend | named | jpause / jresume / jdrop (value: the job id)
    action: str
    value: str = ""


def _act(action: str, value: str = "") -> SettingsAction:
    return SettingsAction(action=action, value=value)


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
        if may_touch(job, user_id, admin):
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
    """The panel page for this viewer; ``admin`` decides which jobs they get buttons for.

    ``user`` is the viewer whose own choices the page shows: None on a panel the whole group sees.
    """
    group = chat.type != "private"
    on, off = t(locale, "settings.on"), t(locale, "settings.off")
    back = button(t(locale, "settings.back"), _act("page", "main"))
    retention = settings.retention
    if page == "retention":
        choices = [button(day_count(locale, d), _act("retention", str(d))) for d in retention.options]
        default = day_count(locale, default_days(chat, retention))
        reset = button(t(locale, "settings.default_days", days=default), _act("retention", "0"))
        return t(locale, "settings.retention_page"), markup(choices[:2], choices[2:], reset, back)
    cancel = button(t(locale, "settings.back"), _act("cancel"))  # a confirmation page's way out
    if page.startswith("shorten:"):
        value = page.removeprefix("shorten:")
        kept = day_count(locale, chosen_days(value, settings) or default_days(chat, retention))
        label = t(locale, "settings.shorten_confirm", days=kept)
        confirm = button(label, _act("shorten", value), style="danger")
        return t(locale, "settings.shorten_page", days=kept), markup(confirm, cancel)
    if page == "notes":
        modes = [button(t(locale, f"settings.notes_{mode}"), _act("notes", mode)) for mode in NOTES_MODES]
        return t(locale, "settings.notes_page"), markup(*modes, back)
    if page == "ambient_off":
        ambient_count = await session.scalar(
            select(func.count()).where(StoredMessage.chat_id == chat.id, StoredMessage.capture == "ambient")
        )
        confirm = button(t(locale, "settings.context_off_confirm"), _act("ambient", "purge"), style="danger")
        return t(locale, "settings.context_off_page", count=ambient_count or 0), markup(confirm, cancel)
    if page == "jobs":
        text, rows = await _jobs_page(session, chat, viewer or (user.id if user else None), admin, locale)
        return text, markup(*rows, back)

    jobs = len(await live_jobs(session, chat.id))
    context = await context_state(bot, chat)
    lines = [
        t(locale, "settings.title", title=literal(one_line(chat.title or t(locale, "settings.this_chat"))))
    ]
    if group:
        lines.append(t(locale, f"settings.context_{context.state}"))
    kept = day_count(locale, kept_days(chat, retention))
    lines.append(t(locale, "settings.retention", days=kept))
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
    lines.append(t(locale, "settings.jobs", count=jobs))
    personal: list[InlineKeyboardButton] = []
    if user is not None:
        spends, named = user.group_spending, user.patron_named
        lines.append(t(locale, "settings.spend_on" if spends else "settings.spend_off"))
        lines.append(t(locale, "settings.named_on" if named else "settings.named_off"))
        personal = [
            button(
                t(locale, "settings.spend_button", state=on if spends else off),
                _act("spend", "off" if spends else "on"),
            ),
            button(
                t(locale, "settings.named_button", state=on if named else off),
                _act("named", "off" if named else "on"),
            ),
        ]
    if group:
        lines.append(t(locale, "settings.admins_only"))

    ambient = None
    if group:
        label = t(locale, "settings.context_button", state=on if chat.ambient else off)
        ambient = button(label, _act("page", "ambient_off") if chat.ambient else _act("ambient", "on"))
    mode = t(locale, f"settings.notes_{chat.notes_mode}")
    free_on = chat.free_models if group else consented(user)
    free = t(locale, "settings.free_button", state=on if free_on else off)
    keyboard = markup(
        link(t(locale, "onboarding.make_admin"), context.admin_url) if context.admin_url else None,
        [ambient, button(t(locale, "settings.retention_button", days=kept), _act("page", "retention"))],
        button(t(locale, "settings.notes_button", mode=mode), _act("page", "notes")),
        button(free, _act("free", "off" if free_on else "on")),
        button(t(locale, "settings.rules_clear"), _act("rules", "clear")) if chat.rules else None,
        button(t(locale, "settings.jobs_button", count=jobs), _act("page", "jobs")) if jobs else None,
        *personal,
    )
    return "\n".join(lines), keyboard


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
                chat.free_models = value == "on"
            case "free" if person is not None:
                on = value == "on"
                person.free_models_consent_at = datetime.now(UTC) if on else None
                person.free_models_consent_version = POLICY_VERSION if on else None
            case "rules":
                chat.rules, chat.rules_draft, chat.rules_draft_by = None, None, None
            case "timezone":
                try:
                    await rezone(session, chat, iana(value), datetime.now(UTC))
                except TimeZoneError:
                    return None
            case _:
                pass
    return "settings.shortened" if shortened else None


async def apply_job(
    db: Database, chat: Chat, user_id: int, admin: bool, action: JobAction, value: str
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
    jobs = action in JOB_ACTIONS or (action == "page" and value == "jobs")
    personal = jobs or action in {"spend", "named"} or (action == "page" and value == "main")
    admin = (jobs or not personal) and await is_admin(bot, chat, press.user_id)
    if not personal and not admin:
        return await press.deny("settings.admins_alert")
    if action == "page":
        page = value
    elif action in JOB_ACTIONS:
        page = "jobs"
        if alert := await apply_job(db, chat, press.user_id, admin, JOB_ACTIONS[action], value):
            await press.deny(alert)  # and the page is redrawn as it is now
    elif action == "retention" and shortens(chat, settings, value):
        page = f"shorten:{value}"  # deletes older history: confirm first
    else:
        page = "main"
        if action != "cancel":
            await press.toast(await apply_setting(db, settings, chat.id, press.user_id, action, value))
    if action == "free" and value == "on" and chat.type != "private" and not chat.free_models:
        await send_markdown(bot, Target(chat.id), t(chat.locale or press.locale, "money.free_enabled"))
    if _ends_confirmation(data) and chat.type != "private" and not press.ephemeral:
        return await press.strip()  # posted in the group on request: it ends here, not as someone's panel
    async with db.session() as session:
        fresh = await session.get(Chat, chat.id)
        own = chat.type == "private" or press.ephemeral  # else the whole group sees it
        user = await session.get(User, press.user_id) if own else None
        if fresh is None:
            return
        text, keyboard = await render(
            session, bot, settings, fresh, user, press.locale, page, admin=admin, viewer=press.user_id
        )
    await press.edit(text, keyboard)


async def on_resume(press: Press, data: ResumeJob, db: Database, bot: Bot) -> None:
    """[▶️ Resume] on a paused job's note: its creator or an admin, checked on the press."""
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None:
        return await press.deny("settings.job_gone")
    admin = await is_admin(bot, chat, press.user_id)
    alert = await apply_job(db, chat, press.user_id, admin, "resume", str(data.id))
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
    return router
