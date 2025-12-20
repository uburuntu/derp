"""Session middleware for resilient Bot API requests.

Adds automatic retry logic for transient Telegram API errors like flood control.
"""

from __future__ import annotations

import asyncio
from typing import Any

import logfire
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import TelegramMethod


class ResilientRequestMiddleware(BaseRequestMiddleware):
    """Middleware that adds retry logic for transient Telegram API errors.

    Handles:
    - TelegramRetryAfter (flood control): waits and retries up to max_retries times
    """

    def __init__(self, max_retries: int = 3):
        super().__init__()
        self.max_retries = max_retries

    async def __call__(
        self,
        make_request,
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        for attempt in range(1, self.max_retries):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt >= self.max_retries:
                    raise

                logfire.warning(
                    "rate_limited_retry",
                    method=type(method).__name__,
                    retry_after=exc.retry_after,
                    attempt=attempt,
                    max_retries=self.max_retries,
                )
                await asyncio.sleep(exc.retry_after)
