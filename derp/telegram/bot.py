"""Bot construction (session, local/fake API base, request policy) and the bot's public @username."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import LinkPreviewOptions

from derp.config import Settings
from derp.telegram.policy import TelegramRequestPolicy

FALLBACK_USERNAME = "DerpRobot"  # what copy calls the bot if Telegram reports no username


def create_bot(settings: Settings) -> Bot:
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(settings.telegram_api_base)
        if settings.telegram_api_base
        else TelegramAPIServer.from_base("https://api.telegram.org")
    )
    session.middleware(TelegramRequestPolicy(heartbeat=settings.heartbeat_path))
    return Bot(
        settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(link_preview=LinkPreviewOptions(is_disabled=True)),
    )


async def bot_username(bot: Bot) -> str:
    """The @username copy and deep links name the bot by."""
    return (await bot.me()).username or FALLBACK_USERNAME
