from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from ..config import settings

router = Router(name="webapp")


def _public_base() -> str:
    if settings.webapp_public_base:
        return settings.webapp_public_base.rstrip("/")
    return f"http://{settings.webapp_host}:{settings.webapp_port}"


@router.message(Command("webapp"))
async def open_webapp(message: Message) -> Message:
    url = f"{_public_base()}/webapp"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Open WebApp", web_app=WebAppInfo(url=url))]]
    )
    return await message.reply("Open the Gemini WebApp:", reply_markup=kb)

