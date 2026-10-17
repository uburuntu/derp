"""/start, /help, the command menus and unknown commands.

/start in private is the feature showcase with Add-to-group and Buy buttons;
``/start buy`` (a deep link) opens the buy menu straight away. /help describes
the chat as it is: how to call Derp, what Derp can see, the wallet that pays,
and the commands. Menus are published per scope at startup; in groups the
commands that show one person's information are ephemeral. A command Derp
doesn't have gets a one-line pointer to /help, without a model call.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardMarkup,
    Message,
)

from derp.agent.invocation import Trigger, TriggerKind, message_text
from derp.billing.menu import buy_button, personal_menu
from derp.billing.patrons import lately_line
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.history.render import one_line
from derp.locales import DEFAULT, LOCALES, resolve, t
from derp.money.command import bought_total, free_left_today
from derp.money.wallets import balances, wallet
from derp.settings.onboarding import context_key
from derp.settings.panel import kept_days
from derp.telegram.buttons import link, markup
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal

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
EPHEMERAL_IN_GROUPS = {"help", "settings", "credits", "info", "memory", "privacy", "terms"}
# Owner console: listed only in the owners' own private chats and /help; plain English, not localized.
OWNER_COMMANDS = {
    "stats": "spend, revenue, pins and scout proposals",
    "feedbacks": "review open feedback",
    "grant": "<user or chat id> <Derpcoins> — gift Derpcoins",
    "block": "<user or chat id> — Derp stays silent there",
    "unblock": "<user or chat id> — undo /block",
    "refund": "<charge id> — refund a Stars payment",
}


async def _username(bot: Bot) -> str:
    return (await bot.me()).username or "DerpRobot"


async def showcase(bot: Bot, settings: Settings, locale: str) -> tuple[str, InlineKeyboardMarkup | None]:
    username = await _username(bot)
    text = t(locale, "start.private", daily=settings.sponsored_daily_credits, username=username)
    add = link(t(locale, "start.add_to_group"), f"https://t.me/{username}?startgroup=derp")
    return text, markup([add, buy_button(locale, group=False)])


async def start(
    message: Message, command: CommandObject, db: Database, settings: Settings, bot: Bot, chat_row: Chat
) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    if message.chat.type != "private":
        # "/start <payload>" arrives when someone adds Derp by link (the join notice covers it);
        # a bare /start may be meant for another bot.
        if not command.args and addressed(message):
            await send_markdown(
                bot, target, await help_text(db, settings, bot, chat_row, message.from_user.id, locale)
            )
        return
    if command.args == "buy":
        text, markup = await personal_menu(db, settings, message.from_user.id, locale)
    else:
        text, markup = await showcase(bot, settings, locale)
    await send_markdown(bot, target, text, reply_markup=markup)


async def help_text(db: Database, settings: Settings, bot: Bot, chat: Chat, user_id: int, locale: str) -> str:
    now = datetime.now(UTC)
    if chat.type == "private":
        async with db.transaction() as session:
            mine = await balances(session, (await wallet(session, "user", user_id)).id, now=now)
            free = await free_left_today(session, settings, user_id, now)
        bought = bought_total(mine)
        plan = mine.get("plan", 0)
        coins = (
            t(
                locale,
                "help.coins_plan",
                plan=f"{plan:,}",
                bought=f"{bought:,}",
                free=free,
                daily=settings.sponsored_daily_credits,
            )
            if plan
            else t(
                locale,
                "help.coins",
                free=free,
                daily=settings.sponsored_daily_credits,
                bought=f"{bought:,}",
            )
        )
        lines = [
            t(locale, "help.title_private"),
            t(locale, "help.private"),
            coins,
            t(locale, "help.commands"),
            t(locale, "help.more_private"),
        ]
        if user_id in settings.owner_ids:
            lines.append(
                "**Owner**\n" + "\n".join(f"/{name} {about}" for name, about in OWNER_COMMANDS.items())
            )
        return "\n\n".join(lines)
    async with db.transaction() as session:
        left = sum((await balances(session, (await wallet(session, "chat", chat.id)).id, now=now)).values())
    username = await _username(bot)
    context = await context_key(bot, chat)
    lines = [
        t(locale, "help.title_group", title=literal(one_line(chat.title or t(locale, "settings.this_chat")))),
        t(locale, "onboarding.summon", username=username),
        t(locale, context, days=kept_days(chat, settings)),
        t(locale, "help.chat_wallet", left=f"{left:,}"),
        await lately_line(db, chat.id, locale),
        t(locale, "help.commands"),
        t(locale, "help.more_group", username=username),
    ]
    return "\n\n".join(line for line in lines if line)


async def show_help(message: Message, db: Database, settings: Settings, bot: Bot, chat_row: Chat) -> None:
    if message.from_user is None or (message.chat.type != "private" and not addressed(message)):
        return
    locale = resolve(message.from_user.language_code)
    text = await help_text(db, settings, bot, chat_row, message.from_user.id, locale)
    await send_markdown(bot, Target.reply_to_message(message), text)


def addressed(message: Message) -> bool:
    """In groups a bare command may be another bot's: answer only ``/cmd@Derp`` or an ephemeral command."""
    return bool(message.ephemeral_message_id) or "@" in message_text(message).split(maxsplit=1)[0]


async def on_unknown_command(message: Message, bot: Bot) -> None:
    if message.chat.type != "private" and not addressed(message):
        return
    name = message_text(message).split(maxsplit=1)[0].removeprefix("/").partition("@")[0][:32]
    locale = resolve(message.from_user.language_code if message.from_user else None)
    await send_markdown(
        bot, Target.reply_to_message(message), t(locale, "help.unknown", command=one_line(name))
    )


async def is_command(message: Message, trigger: Trigger | None = None) -> bool:
    return trigger is not None and trigger.kind is TriggerKind.COMMAND


async def publish_commands(bot: Bot, owner_ids: Sequence[int] = ()) -> None:
    """Set the command menus for private chats and groups in every locale, plus the owners' own; best effort."""
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
        owner_menu = [
            BotCommand(command=name, description=t(DEFAULT, f"commands.{name}")) for name in PRIVATE_COMMANDS
        ]
        owner_menu += [
            BotCommand(command=name, description=about[:256]) for name, about in OWNER_COMMANDS.items()
        ]
        for owner_id in owner_ids:  # a chat scope replaces the private-chat menu for that chat
            await bot.set_my_commands(owner_menu, scope=BotCommandScopeChat(chat_id=owner_id))
    except TelegramAPIError as error:
        log.warning("command menus not published: %s", type(error).__name__)


def build_router() -> Router:
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.message.register(show_help, Command("help"))
    return router


def build_fallback_router() -> Router:
    """Last among command routers: whatever no handler claimed."""
    router = Router(name="unknown-commands")
    router.message.register(on_unknown_command, is_command)
    return router
