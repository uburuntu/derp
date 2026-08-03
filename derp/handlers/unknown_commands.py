"""Recover valid but unrecognized slash commands before chat inference."""

from __future__ import annotations

import re

from aiogram import Bot, Router
from aiogram.filters import BaseFilter, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.db import DatabaseManager, remove_disqualified_message
from derp.history.capture import suppress_outbound_history

router = Router(name="unknown_commands")

_LATE_CHAT_COMMANDS = frozenset({"context", "derp"})
_ANY_SLASH_COMMAND = Command(re.compile(r"^[a-z0-9_]{1,32}$", re.IGNORECASE))


class UnknownSlashCommand(BaseFilter):
    """Match Telegram command syntax while preserving late chat commands."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        match = await _ANY_SLASH_COMMAND(message, bot)
        if not isinstance(match, dict):
            return False
        command = match.get("command")
        if not isinstance(command, CommandObject):
            return False
        return command.command.casefold() not in _LATE_CHAT_COMMANDS


@router.message(UnknownSlashCommand())
async def recover_unknown_command(message: Message, db: DatabaseManager) -> Message:
    """Give command typos a zero-cost recovery path."""
    async with db.session() as session:
        await remove_disqualified_message(
            session,
            chat_telegram_id=message.chat.id,
            telegram_message_id=message.message_id,
        )
    with suppress_outbound_history():
        return await message.reply(_("I don't know that command. Try /help."))


__all__ = ["UnknownSlashCommand", "recover_unknown_command", "router"]
