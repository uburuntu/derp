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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import EditEphemeralMessageText, EditMessageText
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from magic_filter import MagicFilter

from derp.locales import resolve, t
from derp.telegram.bot import group_admin
from derp.telegram.sender import NO_PREVIEW, RichRenderer, rich

type Row = InlineKeyboardButton | Sequence[InlineKeyboardButton | None] | None
type Handler = Callable[..., Awaitable[Any]]


def button(text: str, data: CallbackData, *, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack(), style=style)


class Dismiss(CallbackData, prefix="dismiss"):
    user_id: int | None = None


def dismiss_button(locale: str, *, user_id: int | None = None) -> InlineKeyboardButton:
    """OK for this card's recipient, a specified user, or (on a shared group card) its admins."""
    return button(t(locale, "buttons.ok"), Dismiss(user_id=user_id))


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

    def has_pressed_button(self) -> bool:
        """Telegram may send callback data that isn't on the originating message."""
        keyboard = self.message.reply_markup
        return (
            self.query.data is not None
            and keyboard is not None
            and any(b.callback_data == self.query.data for row in keyboard.inline_keyboard for b in row)
        )

    async def edit(
        self, text: str, keyboard: InlineKeyboardMarkup | None = None, *, render: RichRenderer | None = None
    ) -> None:
        """Replace the pressed message's text (and buttons: none unless given)."""
        content = (render or rich)(text)
        method = (
            EditEphemeralMessageText(
                chat_id=self.chat_id,
                receiver_user_id=self.user_id,
                ephemeral_message_id=self.message.ephemeral_message_id or 0,
                rich_message=content,
                reply_markup=keyboard,
            )
            if self.ephemeral
            else EditMessageText(
                chat_id=self.chat_id,
                message_id=self.message.message_id,
                rich_message=content,
                reply_markup=keyboard,
            )
        )
        with contextlib.suppress(TelegramAPIError):  # unchanged, deleted or an expired ephemeral message
            try:
                await method.emit(self.bot)
            except TelegramBadRequest as error:
                # Formatted cards carry a plain source. A rejected parse can retry the same edit verbatim.
                if render is None or "parse" not in error.message.lower():
                    return
                plain = method.model_copy(
                    update={
                        "rich_message": None,
                        "text": text,
                        "parse_mode": None,
                        "link_preview_options": NO_PREVIEW,
                    }
                )
                await plain.emit(self.bot)

    async def strip(self, keep: Callable[[InlineKeyboardButton], bool] | None = None) -> None:
        """Remove the pressed message's buttons, or only those ``keep`` rejects."""
        current = self.message.reply_markup.inline_keyboard if self.message.reply_markup else []
        keyboard = markup(*([b for b in row if keep(b)] for row in current)) if keep else None
        with contextlib.suppress(TelegramAPIError):
            await self.set_keyboard(keyboard)

    async def take_button(self, *, clear: bool = False, allow_group: bool = False) -> bool:
        """Consume a one-shot control before its side effect. A public-group caller checks its actor and
        chat permissions before opting in. Telegram must accept the markup edit."""
        if not self.has_pressed_button() or self.message.reply_markup is None:
            return False
        receiver = self.message.receiver_user
        if self.ephemeral:
            if receiver is None or receiver.id != self.user_id:
                return False
        elif self.message.chat.type == "private":
            if self.chat_id != self.user_id:
                return False
        elif not allow_group or self.message.chat.type not in {"group", "supergroup"}:
            return False
        rows = self.message.reply_markup.inline_keyboard
        keyboard = (
            None
            if clear
            else markup(*([b for b in row if b.callback_data != self.query.data] for row in rows))
        )
        try:
            await self.set_keyboard(keyboard)
        except TelegramAPIError:
            # Telegram rejects an identical markup edit on a fast double tap. An ambiguous edit
            # also cannot authorize a second send. The command stays available to start again.
            return False
        return True

    async def restore_keyboard(self) -> None:
        """Return a consumed choice after Telegram explicitly refused its follow-up."""
        with contextlib.suppress(TelegramAPIError):
            await self.set_keyboard(self.message.reply_markup)

    async def set_keyboard(self, keyboard: InlineKeyboardMarkup | None) -> None:
        """Replace only the markup, on a public or ephemeral card. Telegram errors reach the caller."""
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
                return await answer_stale(query, stale)
            available = {**deps, "bot": bot}
            try:
                await handler(press, callback_data, **{name: available[name] for name in wanted})
            finally:
                await press.toast()

        router.callback_query.register(on_query, data.filter(rule), *filters)
        return handler

    return register


async def answer_stale(query: CallbackQuery, key: str = "buttons.stale") -> None:
    """The alert for a press nothing can serve: an inaccessible message, or a button no handler knows any more."""
    with contextlib.suppress(TelegramAPIError):
        await query.answer(t(resolve(query.from_user.language_code), key), show_alert=True)


async def on_dismiss(press: Press, data: Dismiss) -> None:
    if not press.has_pressed_button():
        return await press.deny("buttons.stale")
    if data.user_id is not None and data.user_id != press.user_id:
        return await press.deny("buttons.cannot_dismiss")
    if press.ephemeral:
        receiver = press.message.receiver_user
        allowed = receiver is not None and receiver.id == press.user_id
    elif press.message.chat.type == "private":
        allowed = press.chat_id == press.user_id
    else:
        allowed = press.message.chat.type in {"group", "supergroup"} and (
            data.user_id is not None or await group_admin(press.bot, press.chat_id, press.user_id)
        )
    if not allowed:
        return await press.deny("buttons.cannot_dismiss")
    await press.strip()
