"""Method-aware Telegram request policy.

Retries flood-control rejections (bounded) for every method, and network or
server failures only for reads: a send that failed ambiguously may already
have been delivered, so replaying it could duplicate a message or a charge.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType

from derp.telegram.heartbeat import mark_poll_success

# A send that failed this way may still have been delivered: never replay it, and never call it refused.
AMBIGUOUS = (TelegramNetworkError, TelegramServerError, TimeoutError)


def is_read(method: str) -> bool:
    return method.startswith("get")


class TelegramRequestPolicy(BaseRequestMiddleware):
    def __init__(
        self,
        *,
        attempts: int = 3,
        max_retry_after: float = 30,
        heartbeat: Path | None = None,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.attempts = attempts
        self.max_retry_after = max_retry_after
        self.heartbeat = heartbeat
        self.sleep = sleep

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        name = method.__api_method__
        for attempt in range(1, self.attempts + 1):
            try:
                result = await make_request(bot, method)
            except TelegramRetryAfter as error:
                if attempt == self.attempts or error.retry_after > self.max_retry_after:
                    raise
                await self._sleep(error.retry_after)
            except AMBIGUOUS:
                if not is_read(name) or attempt == self.attempts:
                    raise
                await self._sleep(2 ** (attempt - 1))
            else:
                if name == "getUpdates":
                    mark_poll_success(self.heartbeat)
                return result
        raise AssertionError("unreachable: every attempt returns or raises")

    async def _sleep(self, seconds: float) -> None:
        pause = self.sleep(max(seconds, 0))
        if asyncio.iscoroutine(pause):
            await pause
