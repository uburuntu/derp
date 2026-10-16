"""/terms, /privacy and /paysupport: short plain pages, and a line to the owner for payment trouble."""

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from derp.config import Settings
from derp.history.render import one_line
from derp.locales import resolve, t
from derp.ops.owner import notify_owners
from derp.telegram.sender import Target, send_markdown

SUPPORT_CHARS = 1000


async def show_terms(message: Message, bot: Bot) -> None:
    locale = resolve(message.from_user.language_code if message.from_user else None)
    await send_markdown(bot, Target.reply_to_message(message), t(locale, "pages.terms"))


async def show_privacy(message: Message, bot: Bot) -> None:
    locale = resolve(message.from_user.language_code if message.from_user else None)
    await send_markdown(bot, Target.reply_to_message(message), t(locale, "pages.privacy"))


async def pay_support(message: Message, command: CommandObject, bot: Bot, settings: Settings) -> None:
    """The person's own words go to the owners, with their id so the payment can be found."""
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    target = Target.reply_to_message(message)
    text = (command.args or "").strip()
    if not text:
        await send_markdown(bot, target, t(locale, "pages.support_how"))
        return
    who = f"{one_line(message.from_user.full_name)} (id {message.from_user.id})"
    await notify_owners(bot, settings, f"/paysupport from {who}:\n{text[:SUPPORT_CHARS]}")
    await send_markdown(bot, target, t(locale, "pages.support_sent"))


def build_router() -> Router:
    router = Router(name="pages")
    router.message.register(show_terms, Command("terms"))
    router.message.register(show_privacy, Command("privacy"))
    router.message.register(pay_support, Command("paysupport"))
    return router
