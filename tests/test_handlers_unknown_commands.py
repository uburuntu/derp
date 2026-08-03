"""Unknown slash commands recover without entering AI inference."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from derp.handlers.unknown_commands import (
    UnknownSlashCommand,
    recover_unknown_command,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/imagin", "/IMAGIN extra", "/oops_2"])
async def test_unknown_command_filter_matches_valid_command_typos(
    make_message,
    text: str,
) -> None:
    message = make_message(text=text)

    assert await UnknownSlashCommand()(message, message.bot) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "/derp hello",
        "/context",
        "/unknown@SomeOtherBot",
        "put derp anywhere in this message",
    ],
)
async def test_unknown_command_filter_preserves_chat_and_other_bot_routes(
    make_message,
    text: str,
) -> None:
    message = make_message(text=text)

    assert await UnknownSlashCommand()(message, message.bot) is False


@pytest.mark.asyncio
async def test_unknown_command_reply_points_to_help(
    make_message,
    mock_db_client,
) -> None:
    message = make_message(text="/imagin")

    with patch(
        "derp.handlers.unknown_commands.remove_disqualified_message",
        new=AsyncMock(),
    ) as remove_message:
        result = await recover_unknown_command(message, mock_db_client)

    assert result is message
    remove_message.assert_awaited_once()
    message.reply.assert_awaited_once_with("I don't know that command. Try /help.")
