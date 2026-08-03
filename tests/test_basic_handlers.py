"""The start command stays compact and hands off to the unified menu."""

import pytest

from derp.handlers.basic import cmd_start


@pytest.mark.asyncio
async def test_start_is_not_a_feature_or_command_wall(make_message) -> None:
    message = make_message(text="/start")
    message.from_user.full_name = "Ada <Admin>"

    await cmd_start(message)

    text = message.reply.await_args.args[0]
    assert "Ada &lt;Admin&gt;" in text
    assert "/help" in text
    assert "Available commands" not in text
    assert "/set_memory" not in text
