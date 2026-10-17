"""/settings: one panel for the chat's policy and the asker's own choices.

Everyone sees the chat's settings; only admins (anyone, in a private chat)
change them, and the role is checked again on every press. Personal choices
(using my Derpcoins in groups) are each person's own. In groups /settings is
an ephemeral command, so the panel is ephemeral and its buttons edit it
through the ephemeral edit methods.

Turning context off deletes the chat's stored ambient history, so it asks for
confirmation first.
"""

import contextlib
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.models import Message as StoredMessage
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.memory.notes import is_admin
from derp.money.free import POLICY_VERSION, consented
from derp.scheduler.models import Job
from derp.settings.onboarding import can_read_group
from derp.settings.privacy import switch_context
from derp.telegram.sender import Target, rich, send_markdown

RETENTION_CHOICES = (7, 30, 90, 365)
NOTES_MODES = ("auto", "members", "admins", "off")
RULES_PREVIEW = 300


class SettingsAction(CallbackData, prefix="set"):
    action: str  # page | ambient (on / purge) | retention | notes | free | rules | spend
    value: str = ""


def _button(text: str, action: str, value: str = "", *, style: str | None = None) -> InlineKeyboardButton:
    data = SettingsAction(action=action, value=value).pack()
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def kept_days(chat: Chat, settings: Settings) -> int:
    if chat.retention_days:
        return chat.retention_days
    return settings.retention.private_days if chat.type == "private" else settings.retention.group_days


async def render(
    session: AsyncSession,
    settings: Settings,
    chat: Chat,
    user: User | None,
    locale: str,
    page: str = "main",
    *,
    can_read: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    group = chat.type != "private"
    on, off = t(locale, "settings.on"), t(locale, "settings.off")
    back = [_button(t(locale, "settings.back"), "page", "main")]
    if page == "retention":
        default = settings.retention.private_days if not group else settings.retention.group_days
        choices = [
            _button(t(locale, "settings.days", days=d), "retention", str(d)) for d in RETENTION_CHOICES
        ]
        rows = [
            choices[:2],
            choices[2:],
            [_button(t(locale, "settings.default_days", days=default), "retention", "0")],
        ]
        return t(locale, "settings.retention_page"), InlineKeyboardMarkup(inline_keyboard=[*rows, back])
    if page == "notes":
        rows = [[_button(t(locale, f"settings.notes_{mode}"), "notes", mode)] for mode in NOTES_MODES]
        return t(locale, "settings.notes_page"), InlineKeyboardMarkup(inline_keyboard=[*rows, back])
    if page == "ambient_off":
        ambient_count = await session.scalar(
            select(func.count()).where(StoredMessage.chat_id == chat.id, StoredMessage.capture == "ambient")
        )
        confirm = _button(t(locale, "settings.context_off_confirm"), "ambient", "purge", style="danger")
        markup = InlineKeyboardMarkup(inline_keyboard=[[confirm], back])
        return t(locale, "settings.context_off_page", count=ambient_count or 0), markup

    jobs = await session.scalar(
        select(func.count()).where(Job.chat_id == chat.id, Job.state.in_(("active", "paused")))
    )
    lines = [t(locale, "settings.title", title=one_line(chat.title or t(locale, "settings.this_chat")))]
    if group:
        context_line = (
            "settings.context_off"
            if not chat.ambient
            else "settings.context_on"
            if can_read
            else "settings.context_needs_admin"
        )
        lines.append(t(locale, context_line))
    lines.append(t(locale, "settings.retention", days=kept_days(chat, settings)))
    lines.append(t(locale, "settings.notes", mode=t(locale, f"settings.notes_{chat.notes_mode}")))
    if chat.rules:
        lines.append(t(locale, "settings.rules", rules=one_line(chat.rules, RULES_PREVIEW)))
    else:
        lines.append(t(locale, "settings.no_rules"))
    if group:
        lines.append(t(locale, "settings.free_group", state=on if chat.free_models else off))
    else:
        lines.append(t(locale, "settings.free_private", state=on if consented(user) else off))
    lines.append(t(locale, "settings.jobs", count=jobs or 0))
    spends = user is None or user.group_spending
    lines.append(t(locale, "settings.spend_on" if spends else "settings.spend_off"))
    if group:
        lines.append(t(locale, "settings.admins_only"))

    rows: list[list[InlineKeyboardButton]] = []
    first = [
        _button(t(locale, "settings.retention_button", days=kept_days(chat, settings)), "page", "retention")
    ]
    if group:
        label = t(locale, "settings.context_button", state=on if chat.ambient else off)
        first.insert(
            0, _button(label, "page", "ambient_off") if chat.ambient else _button(label, "ambient", "on")
        )
    rows.append(first)
    free_on = chat.free_models if group else consented(user)
    rows.append(
        [
            _button(
                t(locale, "settings.notes_button", mode=t(locale, f"settings.notes_{chat.notes_mode}")),
                "page",
                "notes",
            )
        ]
    )
    rows.append(
        [
            _button(
                t(locale, "settings.free_button", state=on if free_on else off),
                "free",
                "off" if free_on else "on",
            )
        ]
    )
    if chat.rules:
        rows.append([_button(t(locale, "settings.rules_clear"), "rules", "clear")])
    rows.append(
        [
            _button(
                t(locale, "settings.spend_button", state=on if spends else off),
                "spend",
                "off" if spends else "on",
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def on_settings(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    async with db.session() as session:
        chat = await session.get(Chat, message.chat.id)
        user = await session.get(User, message.from_user.id)
        if chat is None:
            return
        can_read = True if chat.type == "private" else await can_read_group(bot, chat)
        text, markup = await render(session, settings, chat, user, locale, can_read=can_read)
    await send_markdown(bot, Target.reply_to_message(message), text, reply_markup=markup)


async def _show(
    bot: Bot, query: CallbackQuery, message: Message, text: str, markup: InlineKeyboardMarkup
) -> None:
    content = rich(text)
    with contextlib.suppress(TelegramAPIError):  # an unchanged panel or an expired ephemeral message
        if message.message_id == 0 and message.ephemeral_message_id:
            await bot.edit_ephemeral_message_text(
                chat_id=message.chat.id,
                receiver_user_id=query.from_user.id,
                ephemeral_message_id=message.ephemeral_message_id,
                rich_message=content,
                reply_markup=markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                rich_message=content,
                reply_markup=markup,
            )


async def apply_setting(db: Database, chat_id: int, user_id: int, action: str, value: str) -> str | None:
    """Change one setting (the caller checked the role); returns an alert key, if any."""
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
            case "ambient":
                chat.ambient = True
            case "retention":
                days = int(value) if value.isdigit() else 0
                chat.retention_days = days if days in RETENTION_CHOICES else None
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
            case _:
                pass
    return None


async def on_action(
    query: CallbackQuery, callback_data: SettingsAction, db: Database, bot: Bot, settings: Settings
) -> None:
    locale = resolve(query.from_user.language_code)
    message = query.message
    if not isinstance(message, Message):
        await query.answer()
        return
    async with db.session() as session:
        chat = await session.get(Chat, message.chat.id)
    if chat is None:
        await query.answer()
        return
    action, value = callback_data.action, callback_data.value
    personal = action == "spend" or (action == "page" and value == "main")
    if not personal and chat.type != "private" and not await is_admin(bot, chat, query.from_user.id):
        await query.answer(t(locale, "settings.admins_alert"), show_alert=True)
        return
    page = value if action == "page" else "main"
    alert = None if action == "page" else await apply_setting(db, chat.id, query.from_user.id, action, value)
    await query.answer(t(locale, alert) if alert else None)
    if action == "free" and value == "on" and chat.type != "private":
        await send_markdown(bot, Target(chat.id), t(chat.locale or locale, "money.free_enabled"))
    async with db.session() as session:
        fresh = await session.get(Chat, chat.id)
        user = await session.get(User, query.from_user.id)
        if fresh is None:
            return
        can_read = True if fresh.type == "private" else await can_read_group(bot, fresh)
        text, markup = await render(session, settings, fresh, user, locale, page, can_read=can_read)
    await _show(bot, query, message, text, markup)


def build_router() -> Router:
    router = Router(name="settings")
    router.message.register(on_settings, Command("settings"))
    actions = {"page", "ambient", "retention", "notes", "free", "rules", "spend"}
    router.callback_query.register(on_action, SettingsAction.filter(F.action.in_(actions)))
    return router
