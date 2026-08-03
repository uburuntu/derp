"""Unsafe premium commands fail closed before reaching general chat."""

from __future__ import annotations

import inspect

import pytest

from derp.filters.meta import MetaCommand
from derp.handlers.premium_suspension import (
    SUSPENDED_PREMIUM_COMMANDS,
    handle_suspended_premium,
    router,
)


@pytest.mark.parametrize(
    "text",
    [
        "/think carefully",
        "/think@DerpTestBot carefully",
        "/video a lighthouse",
        "/video@DerpTestBot a lighthouse",
        "/vid a lighthouse",
        "/veo a lighthouse",
        "a lighthouse #video_fast",
        "a lighthouse #vid_fast",
        "a lighthouse #veo_fast",
    ],
)
async def test_every_suspended_command_and_alias_matches(
    make_message,
    text: str,
) -> None:
    result = await MetaCommand(*SUSPENDED_PREMIUM_COMMANDS)(make_message(text=text))

    assert result is not False


async def test_suspension_reply_needs_no_mutable_domain_dependency(
    make_message,
) -> None:
    message = make_message(text="/think carefully")

    result = await handle_suspended_premium(message)

    assert result is message
    message.reply.assert_awaited_once_with(
        "This feature isn't available right now. You weren't charged."
    )
    assert set(inspect.signature(handle_suspended_premium).parameters) == {"message"}
    assert {handler.callback for handler in router.message.handlers} == {
        handle_suspended_premium
    }
