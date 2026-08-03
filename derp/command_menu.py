"""Durable desired state for Telegram's scoped command menu."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

import logfire
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _

_COMMAND_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")


class CommandAudience(StrEnum):
    """Telegram audiences with independently selected command lists."""

    PRIVATE = "private"
    OPERATOR = "operator"
    GROUP = "group"
    GROUP_ADMIN = "group_admin"


class CommandMenuConfigurationError(RuntimeError):
    """Telegram rejected a command-menu desired-state transition."""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One validated public command and its localized description."""

    command: str
    description: str

    def __post_init__(self) -> None:
        if _COMMAND_PATTERN.fullmatch(self.command) is None:
            raise ValueError(f"invalid Telegram command {self.command!r}")
        description = self.description.strip()
        if not 1 <= len(description) <= 256:
            raise ValueError("Telegram command descriptions must be 1-256 characters")
        object.__setattr__(self, "description", description)

    def telegram(self) -> BotCommand:
        """Materialize a fresh aiogram value for one API request."""
        return BotCommand(command=self.command, description=self.description)


def creation_command_specs() -> tuple[CommandSpec, ...]:
    """Return only creation commands backed by durable paid operations."""
    return (
        CommandSpec("imagine", _("Create an image")),
        CommandSpec("edit", _("Edit an image you reply to")),
        CommandSpec("tts", _("Turn text into speech")),
    )


def command_specs_for(
    audience: CommandAudience,
    *,
    public_purchases_enabled: bool,
) -> tuple[CommandSpec, ...]:
    """Build one complete ordered scope; narrower scopes never inherit deltas."""
    if not isinstance(audience, CommandAudience):
        raise TypeError("audience must be a CommandAudience")
    if not isinstance(public_purchases_enabled, bool):
        raise TypeError("public_purchases_enabled must be a bool")

    creation = creation_command_specs()
    if audience in {CommandAudience.PRIVATE, CommandAudience.OPERATOR}:
        commands = [
            CommandSpec("help", _("See what Derp can do")),
            CommandSpec("settings", _("Manage privacy and history")),
            CommandSpec("privacy", _("Open privacy and deletion controls")),
            CommandSpec("info", _("Inspect a replied-to answer")),
            CommandSpec("terms", _("Review terms for purchases")),
            CommandSpec("support", _("Contact in-bot support")),
            *creation,
            CommandSpec("credits", _("See credits and recent charges")),
        ]
        if public_purchases_enabled:
            commands.append(CommandSpec("buy", _("Buy credits for yourself")))
        commands.extend(
            (
                CommandSpec("plan", _("Manage your monthly plan")),
                CommandSpec("forget", _("Forget a message you reply to")),
            )
        )
        if audience is CommandAudience.OPERATOR:
            commands.insert(0, CommandSpec("operator", _("Open operator console")))
        return tuple(commands)

    settings_description = (
        _("Manage chat settings")
        if audience is CommandAudience.GROUP_ADMIN
        else _("View chat settings")
    )
    commands = [
        CommandSpec("derp", _("Ask Derp")),
        CommandSpec("help", _("See what Derp can do")),
        CommandSpec("settings", settings_description),
        CommandSpec("privacy", _("Open privacy and deletion controls")),
        CommandSpec("info", _("Inspect a replied-to answer")),
        *creation,
        CommandSpec("credits", _("See personal and chat credits")),
    ]
    if public_purchases_enabled:
        commands.append(CommandSpec("buy", _("Buy personal or chat credits")))
    commands.extend((CommandSpec("forget", _("Forget a message you reply to")),))
    return tuple(commands)


def command_menu_language_codes(i18n: I18n) -> tuple[str | None, ...]:
    """Return the language-neutral fallback and every supported ISO locale."""
    language_codes = tuple(sorted(set(i18n.available_locales)))
    invalid = [
        code for code in language_codes if re.fullmatch(r"[a-z]{2}", code) is None
    ]
    if invalid:
        raise ValueError("Telegram command locales must be ISO 639-1 language codes")
    return (None, *language_codes)


async def configure_bot_command_menu(
    bot: Bot,
    *,
    i18n: I18n,
    public_purchases_enabled: bool,
    operator_ids: Collection[int] = (),
) -> None:
    """Converge Telegram's server-side command and private-menu state."""
    if any(
        isinstance(operator_id, bool)
        or not isinstance(operator_id, int)
        or operator_id <= 0
        for operator_id in operator_ids
    ):
        raise ValueError("operator_ids must contain only positive integers")
    operators = tuple(sorted(set(operator_ids)))
    languages = command_menu_language_codes(i18n)
    audiences = (
        (CommandAudience.PRIVATE, BotCommandScopeAllPrivateChats()),
        (CommandAudience.GROUP, BotCommandScopeAllGroupChats()),
        (CommandAudience.GROUP_ADMIN, BotCommandScopeAllChatAdministrators()),
    )

    for language_code in languages:
        locale = language_code or i18n.default_locale
        with i18n.context(), i18n.use_locale(locale):
            desired = {
                audience: [
                    spec.telegram()
                    for spec in command_specs_for(
                        audience,
                        public_purchases_enabled=public_purchases_enabled,
                    )
                ]
                for audience, _ in audiences
            }
            desired[CommandAudience.OPERATOR] = [
                spec.telegram()
                for spec in command_specs_for(
                    CommandAudience.OPERATOR,
                    public_purchases_enabled=public_purchases_enabled,
                )
            ]
        for audience, scope in audiences:
            configured = await bot.set_my_commands(
                commands=desired[audience],
                scope=scope,
                language_code=language_code,
            )
            _require_configured(configured, f"set_{audience.value}_commands")
        for operator_id in operators:
            configured = await bot.set_my_commands(
                commands=desired[CommandAudience.OPERATOR],
                scope=BotCommandScopeChat(chat_id=operator_id),
                language_code=language_code,
            )
            _require_configured(configured, "set_operator_commands")

    for language_code in languages:
        deleted = await bot.delete_my_commands(
            scope=BotCommandScopeDefault(),
            language_code=language_code,
        )
        _require_configured(deleted, "delete_default_commands")

    menu_configured = await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    _require_configured(menu_configured, "set_private_command_button")
    logfire.info(
        "telegram.command_menu_configured",
        scope_count=len(audiences) + len(operators),
        operator_scope_count=len(operators),
        language_variant_count=len(languages),
        public_purchases_enabled=public_purchases_enabled,
    )


def _require_configured(result: bool, action: str) -> None:
    if result is not True:
        raise CommandMenuConfigurationError(f"Telegram did not complete {action}")


__all__ = [
    "CommandAudience",
    "CommandMenuConfigurationError",
    "CommandSpec",
    "command_menu_language_codes",
    "command_specs_for",
    "configure_bot_command_menu",
    "creation_command_specs",
]
