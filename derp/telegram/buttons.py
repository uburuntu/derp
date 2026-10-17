"""Inline buttons: building keyboards and handling presses the same way everywhere.

State lives in callback data and database rows, never in memory, so buttons keep
working across deploys. ``on_press`` registers a handler that receives a ``Press``:
the query with its (accessible) message and locale, plus the edits every flow needs —
rich and ephemeral-aware, with "not modified" and expired messages ignored. Every
press is answered exactly once: ``deny`` (an alert) and ``toast`` answer it, and a
handler that answers nothing gets a silent answer, so a button never spins. A press
on a message Telegram no longer shows gets the ``stale`` alert. Permission rules stay
in each handler (``if not allowed: return await press.deny(key)``); ``deny`` returns
instead of raising, so it never rolls back the handler's transaction.
"""

import annotationlib
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from magic_filter import MagicFilter

from derp.locales import resolve, t
from derp.telegram.sender import rich

type Row = InlineKeyboardButton | Sequence[InlineKeyboardButton | None] | None
type Handler = Callable[..., Awaitable[Any]]


def button(text: str, data: CallbackData, *, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack(), style=style)


def link(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def markup(*rows: Row) -> InlineKeyboardMarkup | None:
    """A keyboard from rows; ``None`` rows and buttons are dropped, a bare button is its own row."""
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        buttons = [row] if isinstance(row, InlineKeyboardButton) else [b for b in row or () if b is not None]
        if buttons:
            keyboard.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


@dataclass(slots=True)
class Press:
    query: CallbackQuery
    message: Message
    bot: Bot
    locale: str
    answered: bool = False

    @property
    def user_id(self) -> int:
        return self.query.from_user.id

    @property
    def chat_id(self) -> int:
        return self.message.chat.id

    @property
    def ephemeral(self) -> bool:
        return self.message.message_id == 0 and bool(self.message.ephemeral_message_id)

    async def edit(self, markdown: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        """Replace the pressed message's text (and buttons: none unless given)."""
        with contextlib.suppress(TelegramAPIError):  # unchanged, deleted or an expired ephemeral message
            if self.ephemeral:
                await self.bot.edit_ephemeral_message_text(
                    chat_id=self.chat_id,
                    receiver_user_id=self.user_id,
                    ephemeral_message_id=self.message.ephemeral_message_id or 0,
                    rich_message=rich(markdown),
                    reply_markup=keyboard,
                )
            else:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message.message_id,
                    rich_message=rich(markdown),
                    reply_markup=keyboard,
                )

    async def strip(self, keep: Callable[[InlineKeyboardButton], bool] | None = None) -> None:
        """Remove the pressed message's buttons, or only those ``keep`` rejects."""
        current = self.message.reply_markup.inline_keyboard if self.message.reply_markup else []
        keyboard = markup(*([b for b in row if keep(b)] for row in current)) if keep else None
        with contextlib.suppress(TelegramAPIError):
            if self.ephemeral:
                await self.bot.edit_ephemeral_message_reply_markup(
                    chat_id=self.chat_id,
                    receiver_user_id=self.user_id,
                    ephemeral_message_id=self.message.ephemeral_message_id or 0,
                    reply_markup=keyboard,
                )
            else:
                await self.bot.edit_message_reply_markup(
                    chat_id=self.chat_id, message_id=self.message.message_id, reply_markup=keyboard
                )

    async def deny(self, key: str, **params: object) -> None:
        """Refuse with an alert (a localized ``key``)."""
        await self.answer(t(self.locale, key, **params), alert=True)

    async def toast(self, key: str | None = None, **params: object) -> None:
        """Answer with a brief notice, or silently without a ``key``."""
        await self.answer(t(self.locale, key, **params) if key else None, alert=False)

    async def answer(self, text: str | None, *, alert: bool = False) -> None:
        """Answer with ready text (owner copy is not localized); only the first answer counts."""
        if self.answered:
            return
        self.answered = True
        with contextlib.suppress(TelegramAPIError):  # the query expired: nothing left to answer
            await self.query.answer(text, show_alert=alert)


def on_press(
    router: Router,
    data: type[CallbackData],
    rule: MagicFilter | None = None,
    *filters: Any,
    stale: str = "buttons.stale",
) -> Callable[[Handler], Handler]:
    """Register ``handler(press, data, **deps)`` for presses of ``data`` buttons (matching ``rule``).

    ``deps`` are any of aiogram's handler data by name (``db``, ``settings``, ``runtime``…).
    """

    def register(handler: Handler) -> Handler:
        # names only: annotations may name types imported for type checking alone
        signature = inspect.signature(handler, annotation_format=annotationlib.Format.FORWARDREF)
        wanted = list(signature.parameters)[2:]

        async def on_query(query: CallbackQuery, callback_data: CallbackData, bot: Bot, **deps: Any) -> None:
            message = query.message
            press = (
                Press(query, message, bot, resolve(query.from_user.language_code))
                if isinstance(message, Message)
                else None
            )
            if press is None:
                with contextlib.suppress(TelegramAPIError):
                    await query.answer(t(resolve(query.from_user.language_code), stale), show_alert=True)
                return
            available = {**deps, "bot": bot}
            try:
                await handler(press, callback_data, **{name: available[name] for name in wanted})
            finally:
                await press.toast()

        router.callback_query.register(on_query, data.filter(rule), *filters)
        return handler

    return register
