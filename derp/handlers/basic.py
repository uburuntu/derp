from aiogram import Router, html
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    return await message.reply(
        _("Hello, {name}!").format(name=html.quote(message.from_user.full_name))
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    return await message.reply(_("I'm a friendly AI-powered Telegram bot"))
