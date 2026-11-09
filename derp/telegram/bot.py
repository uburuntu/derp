"""Bot construction (session, local/fake API base, request policy) and the bot's @usernames.

A bot can have secondary usernames besides the one ``getMe`` reports, and a mention or ``/cmd@name`` by any
of them reaches it; only ``getChat`` on the bot itself lists them all (``active_usernames``).
"""

from weakref import WeakKeyDictionary

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions

from derp.config import Settings
from derp.telegram.policy import TelegramRequestPolicy

FALLBACK_USERNAME = "DerpRobot"  # what copy calls the bot if Telegram reports no username
_usernames: WeakKeyDictionary[Bot, tuple[str, ...]] = WeakKeyDictionary()


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


async def bot_usernames(bot: Bot) -> tuple[str, ...]:
    """Every @username that reaches the bot, ``getMe``'s first; asked once per process (a failed ask is retried)."""
    if (known := _usernames.get(bot)) is not None:
        return known
    me = await bot.me()
    own = (me.username,) if me.username else ()
    try:
        names = (await bot.get_chat(me.id)).active_usernames or []
    except TelegramAPIError:
        return own
    known = _usernames[bot] = tuple(dict.fromkeys([*own, *names]))
    return known
