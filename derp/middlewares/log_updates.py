"""Privacy-safe root tracing for inbound Telegram updates."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

import logfire
from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import TelegramObject, Update
from aiogram.types.update import UpdateTypeLookupError
from opentelemetry.trace import SpanKind

from derp.common.update_context import UpdateContext, update_ctx
from derp.observability import redacted_exception


class LogUpdatesMiddleware(BaseMiddleware):
    """Trace one update without recording Telegram or user content."""

    def __init__(self, logfire_instance: logfire.Logfire | None = None) -> None:
        self.logfire = logfire_instance or logfire.DEFAULT_LOGFIRE_INSTANCE

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            raise RuntimeError("Got an unexpected event type")

        event_context = UserContextMiddleware.resolve_event_context(event)
        update_type = _update_type(event)
        message_id = _message_id(event)
        attributes: dict[str, str | int] = {
            "telegram.update_id": event.update_id,
            "telegram.update_type": update_type,
        }
        optional_attributes = {
            "telegram.chat_id": event_context.chat_id,
            "telegram.user_id": event_context.user_id,
            "telegram.message_id": message_id,
            "telegram.thread_id": event_context.thread_id,
        }
        attributes.update(
            {
                key: value
                for key, value in optional_attributes.items()
                if value is not None
            }
        )

        context_token = update_ctx.set(
            UpdateContext(
                update_id=event.update_id,
                chat_id=event_context.chat_id,
                user_id=event_context.user_id,
                thread_id=event_context.thread_id,
            )
        )
        try:
            failure: BaseException | None = None
            failure_traceback: TracebackType | None = None
            response: Any = None
            with self.logfire.span(
                "telegram.update",
                _span_kind=SpanKind.CONSUMER,
                **attributes,
            ) as span:
                try:
                    response = await handler(event, data)
                except asyncio.CancelledError as exc:
                    failure = exc
                    failure_traceback = exc.__traceback__
                    span.set_attribute("derp.update_cancelled", True)
                except BaseException as exc:
                    failure = exc
                    failure_traceback = exc.__traceback__
                    span.set_level("error")
                    span.set_attribute("error.type", type(exc).__name__)
                    span.record_exception(
                        redacted_exception(exc),
                        attributes={"exception.type": type(exc).__name__},
                        escaped=True,
                    )
                else:
                    span.set_attribute("derp.update_handled", response is not UNHANDLED)

            if failure is not None:
                raise failure.with_traceback(failure_traceback)
            return response
        finally:
            update_ctx.reset(context_token)


def _update_type(update: Update) -> str:
    try:
        return update.event_type
    except UpdateTypeLookupError:
        return "unknown"


def _message_id(update: Update) -> int | None:
    try:
        event = update.event
    except UpdateTypeLookupError:
        return None

    if (message_id := getattr(event, "message_id", None)) is not None:
        return message_id
    if message := getattr(event, "message", None):
        return message.message_id
    return None
