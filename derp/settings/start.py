"""/start and /help."""

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from derp.telegram.sender import Target, send_markdown

router = Router(name="start")


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, bot: Bot) -> None:
    await send_markdown(bot, Target.reply_to_message(message), "**Derp** is waking up. Say something.")
