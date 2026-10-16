"""/start, /help, the command menus and unknown commands.

/start in private is the feature showcase with Add-to-group and Buy buttons;
``/start buy`` (a deep link) opens the buy menu straight away. /help describes
the chat as it is: how to call Derp, what Derp can see, the wallet that pays,
and the commands. Menus are published per scope at startup; in groups the
commands that show one person's information are ephemeral. A command Derp
doesn't have gets a one-line pointer to /help, without a model call.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from derp.agent.invocation import Trigger, TriggerKind, message_text
from derp.billing.command import personal_menu
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.history.render import one_line
from derp.locales import DEFAULT, LOCALES, resolve, t
from derp.money.command import free_left_today
from derp.money.wallets import balances, wallet
from derp.settings.onboarding import sees_everything
from derp.settings.panel import kept_days
from derp.telegram.sender import Target, send_markdown

log = logging.getLogger("derp.start")

PRIVATE_COMMANDS = (
    "start",
    "help",
    "buy",
    "credits",
    "settings",
    "memory",
    "info",
    "forget",
    "feedback",
    "privacy",
    "terms",
    "paysupport",
)
GROUP_COMMANDS = (
    "help",
    "settings",
    "credits",
    "buy",
    "memory",
    "info",
    "forget",
    "feedback",
    "privacy",
    "terms",
)
EPHEMERAL_IN_GROUPS = {"help", "settings", "credits", "info", "privacy", "terms"}


class StartAction(CallbackData, prefix="start"):
    action: str  # buy


async def _username(bot: Bot) -> str:
    return (await bot.me()).username or "DerpRobot"


async def showcase(bot: Bot, settings: Settings, locale: str) -> tuple[str, InlineKeyboardMarkup]:
    username = await _username(bot)
    text = t(locale, "start.private", daily=settings.sponsored_daily_credits, username=username)
    add = InlineKeyboardButton(
        text=t(locale, "start.add_to_group"), url=f"https://t.me/{username}?startgroup=derp"
    )
    buy = InlineKeyboardButton(text=t(locale, "start.buy"), callback_data=StartAction(action="buy").pack())
    return text, InlineKeyboardMarkup(inline_keyboard=[[add, buy]])


async def start(
    message: Message, command: CommandObject, db: Database, settings: Settings, bot: Bot, chat_row: Chat
) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    if message.chat.type != "private":
        if (
            not command.args
        ):  # "/start <payload>" arrives when someone adds Derp by link; the join notice covers it
            await send_markdown(
                bot, target, await help_text(db, settings, bot, chat_row, message.from_user.id, locale)
            )
        return
    if command.args == "buy":
        text, markup = await personal_menu(db, settings, message.from_user.id, locale)
    else:
        text, markup = await showcase(bot, settings, locale)
    await send_markdown(bot, target, text, reply_markup=markup)


async def on_start_action(query: CallbackQuery, db: Database, settings: Settings, bot: Bot) -> None:
    locale = resolve(query.from_user.language_code)
    await query.answer()
    text, markup = await personal_menu(db, settings, query.from_user.id, locale)
    await send_markdown(bot, Target(query.from_user.id), text, reply_markup=markup)


async def help_text(db: Database, settings: Settings, bot: Bot, chat: Chat, user_id: int, locale: str) -> str:
    now = datetime.now(UTC)
    if chat.type == "private":
        async with db.transaction() as session:
            mine = await balances(session, (await wallet(session, "user", user_id)).id, now=now)
            free = await free_left_today(session, settings, user_id, now)
        bought = sum(mine.values())
        lines = [
            t(locale, "help.title_private"),
            t(locale, "help.private"),
            t(locale, "help.coins", free=free, daily=settings.sponsored_daily_credits, bought=f"{bought:,}"),
            t(locale, "help.commands"),
            t(locale, "help.more_private"),
        ]
        return "\n\n".join(lines)
    async with db.transaction() as session:
        left = sum((await balances(session, (await wallet(session, "chat", chat.id)).id, now=now)).values())
    username = await _username(bot)
    context = "onboarding.context_on" if await sees_everything(bot, chat) else "onboarding.context_off"
    lines = [
        t(locale, "help.title_group", title=one_line(chat.title or t(locale, "settings.this_chat"))),
        t(locale, "onboarding.summon", username=username),
        t(locale, context, days=kept_days(chat, settings)),
        t(locale, "help.chat_wallet", left=f"{left:,}"),
        t(locale, "help.commands"),
        t(locale, "help.more_group", username=username),
    ]
    return "\n\n".join(lines)


async def show_help(message: Message, db: Database, settings: Settings, bot: Bot, chat_row: Chat) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    text = await help_text(db, settings, bot, chat_row, message.from_user.id, locale)
    await send_markdown(bot, Target.reply_to_message(message), text)


async def on_unknown_command(message: Message, bot: Bot) -> None:
    """Private chats always; groups only for commands addressed to Derp, since bare ones may be another bot's."""
    word = message_text(message).split(maxsplit=1)[0]
    if message.chat.type != "private" and "@" not in word:
        return
    name = word.removeprefix("/").partition("@")[0][:32]
    locale = resolve(message.from_user.language_code if message.from_user else None)
    await send_markdown(
        bot, Target.reply_to_message(message), t(locale, "help.unknown", command=one_line(name))
    )


async def is_command(message: Message, trigger: Trigger | None = None) -> bool:
    return trigger is not None and trigger.kind is TriggerKind.COMMAND


async def publish_commands(bot: Bot) -> None:
    """Set the command menus for private chats and groups in every locale; best effort."""
    scopes = (
        (BotCommandScopeAllPrivateChats(), PRIVATE_COMMANDS, set[str]()),
        (BotCommandScopeAllGroupChats(), GROUP_COMMANDS, EPHEMERAL_IN_GROUPS),
    )
    try:
        for locale in LOCALES:
            for scope, names, ephemeral in scopes:
                commands = [
                    BotCommand(
                        command=name,
                        description=t(locale, f"commands.{name}"),
                        is_ephemeral=True if name in ephemeral else None,
                    )
                    for name in names
                ]
                await bot.set_my_commands(
                    commands, scope=scope, language_code=None if locale == DEFAULT else locale
                )
    except TelegramAPIError as error:
        log.warning("command menus not published: %s", type(error).__name__)


def build_router() -> Router:
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.message.register(show_help, Command("help"))
    router.callback_query.register(on_start_action, StartAction.filter(F.action == "buy"))
    return router


def build_fallback_router() -> Router:
    """Last among command routers: whatever no handler claimed."""
    router = Router(name="unknown-commands")
    router.message.register(on_unknown_command, is_command)
    return router
