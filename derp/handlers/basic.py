from aiogram import Router, html
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    name = html.quote(message.from_user.full_name) if message.from_user else "there"
    welcome_text = _(
        "Hello, {name}. Ask me naturally here, or open /help for context, "
        "privacy, creation, and credit controls."
    ).format(name=name)
    return await message.reply(welcome_text)
