"""aiogram's command filters, taking ``/cmd@name`` for every @username of the bot (``bot.bot_usernames``).

aiogram compares the mention with ``getMe``'s username only, so ``/settings@`` a secondary username would
fall through to no handler. Register commands with these, never ``aiogram.filters.Command``.
"""

from aiogram import Bot, filters
from aiogram.filters import CommandObject
from aiogram.filters.command import CommandException

from derp.telegram.bot import bot_usernames


def _named(mention: str, usernames: tuple[str, ...]) -> bool:
    return mention.casefold() in {name.casefold() for name in usernames}


class Command(filters.Command):
    __slots__ = ()

    async def validate_mention(self, bot: Bot, command: CommandObject) -> None:
        mention = command.mention
        if mention and not self.ignore_mention and not _named(mention, await bot_usernames(bot)):
            raise CommandException("Mention did not match")


class CommandStart(Command, filters.CommandStart):
    __slots__ = ()
