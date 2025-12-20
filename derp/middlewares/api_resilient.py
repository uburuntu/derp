"""Session middleware for resilient Bot API requests.

Adds automatic retry logic for transient Telegram API errors like flood control
and HTML parse failures.
"""

from __future__ import annotations

import asyncio
from typing import Any

import logfire
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMediaGroup, TelegramMethod

from derp.common.sanitize import strip_html_tags


def _create_plain_text_method(
    method: TelegramMethod[Any],
) -> TelegramMethod[Any] | None:
    """Create a copy of the method with parse_mode=None and stripped text/caption.

    Returns None if the method doesn't have text/caption content.
    """
    # Handle SendMediaGroup specially (has list of media items)
    if isinstance(method, SendMediaGroup):
        new_media = [
            item.model_copy(
                update={"caption": strip_html_tags(caption), "parse_mode": None}
            )
            if (caption := getattr(item, "caption", None))
            else item
            for item in method.media
        ]
        return method.model_copy(update={"media": new_media})

    # Handle methods with text or caption field (mutually exclusive)
    if text := getattr(method, "text", None):
        return method.model_copy(
            update={"text": strip_html_tags(text), "parse_mode": None}
        )

    if caption := getattr(method, "caption", None):
        return method.model_copy(
            update={"caption": strip_html_tags(caption), "parse_mode": None}
        )

    return None


class ResilientRequestMiddleware(BaseRequestMiddleware):
    """Middleware that adds retry logic for transient Telegram API errors.

    Handles:
    - TelegramRetryAfter (flood control): waits and retries up to max_retries times
    - TelegramBadRequest with "can't parse entities": retries with plain text
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
        last_exception: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
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
                last_exception = exc

            except TelegramBadRequest as exc:
                if "can't parse entities" not in exc.message.lower():
                    raise

                # Try with plain text fallback
                plain_method = _create_plain_text_method(method)
                if plain_method is None:
                    raise

                logfire.warning(
                    "html_parse_failed_fallback",
                    method=type(method).__name__,
                    _exc_info=True,
                )
                return await make_request(bot, plain_method)

        # Should not reach here, but satisfy type checker
        if last_exception:
            raise last_exception
        raise RuntimeError("Unexpected state in ResilientRequestMiddleware")
