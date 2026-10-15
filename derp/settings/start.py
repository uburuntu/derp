"""/start and /help."""

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from derp.locales import t
from derp.telegram.sender import Target, send_markdown

router = Router(name="start")


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, bot: Bot) -> None:
    locale = message.from_user.language_code if message.from_user else None
    await send_markdown(bot, Target.reply_to_message(message), t(locale, "start.private"))
