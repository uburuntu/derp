from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    return await message.reply(f"Hello, {message.from_user.full_name}!")


@router.message(Command("help"))
async def cmd_help(message: Message):
    return await message.reply("I'm a friendly AI-powered Telegram bot")
