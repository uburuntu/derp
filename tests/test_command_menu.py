"""Telegram's command index converges to one scoped desired state."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.types import (
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)
from aiogram.utils.i18n import I18n

from derp.command_menu import (
    CommandAudience,
    CommandMenuConfigurationError,
    command_menu_language_codes,
    command_specs_for,
    configure_bot_command_menu,
    creation_command_specs,
)
from derp.handlers.premium_suspension import SUSPENDED_PREMIUM_COMMANDS

_PRIVATE_COMMANDS = (
    "help",
    "settings",
    "privacy",
    "terms",
    "support",
    "paysupport",
    "imagine",
    "edit",
    "tts",
    "credits",
    "plan",
    "forget",
)
_OPERATOR_COMMANDS = ("operator", *_PRIVATE_COMMANDS)
_GROUP_COMMANDS = (
    "derp",
    "help",
    "settings",
    "privacy",
    "imagine",
    "edit",
    "tts",
    "credits",
    "forget",
)


def _command_names(
    audience: CommandAudience,
    *,
    public_purchases_enabled: bool = False,
) -> tuple[str, ...]:
    return tuple(
        spec.command
        for spec in command_specs_for(
            audience,
            public_purchases_enabled=public_purchases_enabled,
        )
    )


@pytest.mark.parametrize(
    ("audience", "expected"),
    [
        (CommandAudience.PRIVATE, _PRIVATE_COMMANDS),
        (CommandAudience.OPERATOR, _OPERATOR_COMMANDS),
        (CommandAudience.GROUP, _GROUP_COMMANDS),
        (CommandAudience.GROUP_ADMIN, _GROUP_COMMANDS),
    ],
)
def test_command_scopes_expose_only_canonical_live_paths(
    audience: CommandAudience,
    expected: tuple[str, ...],
) -> None:
    names = _command_names(audience)

    assert names == expected
    assert not set(names).intersection(SUSPENDED_PREMIUM_COMMANDS)
    assert not set(names).intersection(
        {
            "balance",
            "debug_help",
            "ed",
            "image",
            "img",
            "say",
            "subscription",
            "voice",
            "donate",
        }
    )


def test_support_is_private_and_donations_are_suspended() -> None:
    private = _command_names(CommandAudience.PRIVATE)
    group = _command_names(CommandAudience.GROUP)

    assert ("support", "paysupport") == (
        private[private.index("support")],
        private[private.index("paysupport")],
    )
    assert "support" not in group
    assert "paysupport" not in group
    assert all("donate" not in _command_names(audience) for audience in CommandAudience)


def test_purchase_commands_follow_public_intake_and_chat_scope() -> None:
    assert "buy" not in _command_names(CommandAudience.PRIVATE)
    assert "buy_chat" not in _command_names(CommandAudience.GROUP)

    private = _command_names(
        CommandAudience.PRIVATE,
        public_purchases_enabled=True,
    )
    group = _command_names(
        CommandAudience.GROUP,
        public_purchases_enabled=True,
    )
    admin = _command_names(
        CommandAudience.GROUP_ADMIN,
        public_purchases_enabled=True,
    )
    operator = _command_names(
        CommandAudience.OPERATOR,
        public_purchases_enabled=True,
    )

    assert private[private.index("credits") + 1] == "buy"
    assert "buy_chat" not in private
    assert group[group.index("credits") + 1] == "buy_chat"
    assert "buy" not in group
    assert admin == group
    assert operator[0] == "operator"
    assert operator[operator.index("credits") + 1] == "buy"


def test_creation_catalog_has_only_durable_media_operations() -> None:
    specs = creation_command_specs()

    assert tuple(spec.command for spec in specs) == ("imagine", "edit", "tts")
    assert all(spec.description for spec in specs)
    assert all(spec.telegram().is_ephemeral is None for spec in specs)


@pytest.mark.asyncio
async def test_configuration_replaces_every_supported_scope_and_default(
    setup_i18n: I18n,
) -> None:
    bot = MagicMock(spec=Bot)
    bot.set_my_commands = AsyncMock(return_value=True)
    bot.delete_my_commands = AsyncMock(return_value=True)
    bot.set_chat_menu_button = AsyncMock(return_value=True)

    await configure_bot_command_menu(
        bot,
        i18n=setup_i18n,
        public_purchases_enabled=False,
    )

    languages = command_menu_language_codes(setup_i18n)
    expected_by_scope = {
        BotCommandScopeAllPrivateChats: _PRIVATE_COMMANDS,
        BotCommandScopeAllGroupChats: _GROUP_COMMANDS,
        BotCommandScopeAllChatAdministrators: _GROUP_COMMANDS,
    }
    configured: dict[tuple[type[object], str | None], tuple[str, ...]] = {}
    for call in bot.set_my_commands.await_args_list:
        commands = call.kwargs["commands"]
        scope = call.kwargs["scope"]
        language_code = call.kwargs["language_code"]
        configured[(type(scope), language_code)] = tuple(
            command.command for command in commands
        )
        assert all(command.is_ephemeral is None for command in commands)

    assert configured == {
        (scope_type, language_code): names
        for language_code in languages
        for scope_type, names in expected_by_scope.items()
    }
    assert [
        (type(call.kwargs["scope"]), call.kwargs["language_code"])
        for call in bot.delete_my_commands.await_args_list
    ] == [(BotCommandScopeDefault, language_code) for language_code in languages]
    bot.set_chat_menu_button.assert_awaited_once()
    menu_button = bot.set_chat_menu_button.await_args.kwargs["menu_button"]
    assert isinstance(menu_button, MenuButtonCommands)


@pytest.mark.asyncio
async def test_configuration_fails_fast_when_telegram_rejects_state(
    setup_i18n: I18n,
) -> None:
    bot = MagicMock(spec=Bot)
    bot.set_my_commands = AsyncMock(return_value=False)
    bot.delete_my_commands = AsyncMock(return_value=True)
    bot.set_chat_menu_button = AsyncMock(return_value=True)

    with pytest.raises(CommandMenuConfigurationError, match="set_private_commands"):
        await configure_bot_command_menu(
            bot,
            i18n=setup_i18n,
            public_purchases_enabled=False,
        )

    bot.delete_my_commands.assert_not_awaited()
    bot.set_chat_menu_button.assert_not_awaited()


@pytest.mark.asyncio
async def test_configuration_sets_complete_operator_scopes_per_locale(
    setup_i18n: I18n,
) -> None:
    bot = MagicMock(spec=Bot)
    bot.set_my_commands = AsyncMock(return_value=True)
    bot.delete_my_commands = AsyncMock(return_value=True)
    bot.set_chat_menu_button = AsyncMock(return_value=True)

    await configure_bot_command_menu(
        bot,
        i18n=setup_i18n,
        public_purchases_enabled=False,
        operator_ids={42, 7},
    )

    operator_calls = [
        call
        for call in bot.set_my_commands.await_args_list
        if isinstance(call.kwargs["scope"], BotCommandScopeChat)
    ]
    assert [call.kwargs["scope"].chat_id for call in operator_calls] == [
        operator_id
        for _ in command_menu_language_codes(setup_i18n)
        for operator_id in (7, 42)
    ]
    assert all(
        tuple(command.command for command in call.kwargs["commands"])
        == _OPERATOR_COMMANDS
        for call in operator_calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_ids", [{0}, {-1}, {True}])
async def test_configuration_rejects_invalid_operator_ids(
    setup_i18n: I18n,
    operator_ids: set[int],
) -> None:
    bot = MagicMock(spec=Bot)

    with pytest.raises(ValueError, match="positive integers"):
        await configure_bot_command_menu(
            bot,
            i18n=setup_i18n,
            public_purchases_enabled=False,
            operator_ids=operator_ids,
        )
