import time

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import (
    TelegramObject,
    Update,
)
from ..common.tg import chat_info, decompose_update, user_info
from ..common.utils import get_logger


class LogUpdatesMiddleware(BaseMiddleware):
    """
    Extracts essential info from the Telegram update and prints a log
    """

    def __init__(self):
        self.logger = get_logger("Update")

    @classmethod
    def log_string(cls, update: Update, elapsed_ms: int) -> str:
        f, user, sender_chat, chat, info = decompose_update(update)

        user = user and user_info(user, sender_chat)
        chat = chat and chat_info(chat)

        chat = f" | {chat}" if chat else ""
        user = f" | {user}" if user else ""
        timeout = f" [{elapsed_ms:>4} ms]"

        return f"{f.__class__.__name__}{timeout}{chat}{user} | {info}"

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            raise RuntimeError("Got an unexpected event type")

        start_time = time.monotonic()

        response = await handler(event, data)

        elapsed_ms = round((time.monotonic() - start_time) * 1000)
        log = self.log_string(update=event, elapsed_ms=elapsed_ms)

        if response is UNHANDLED:
            return self.logger.debug(log)

        return self.logger.info(log)
