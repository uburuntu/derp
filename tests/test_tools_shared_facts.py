"""Shared-fact proposals are scoped data and require native human review."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from derp.history.capture import capture_outbound_history, should_capture_outbound
from derp.tools.policy import ChatTool
from derp.tools.shared_facts import (
    SharedFactCallback,
    SharedFactTools,
    propose_shared_fact,
)


@pytest.mark.asyncio
async def test_proposal_is_scoped_and_control_message_is_not_history() -> None:
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)
    message = MagicMock(message_thread_id=77)

    async def reply(*args, **kwargs):
        assert not should_capture_outbound()
        return MagicMock()

    message.reply = AsyncMock(side_effect=reply)
    deps = SimpleNamespace(
        db=db,
        message=message,
        chat_model=SimpleNamespace(id=UUID(int=1)),
        user_model=SimpleNamespace(id=UUID(int=2)),
    )
    ctx = SimpleNamespace(deps=deps)
    proposal = SimpleNamespace(id=UUID(int=3), fact_text="The release is Friday.")

    with (
        patch(
            "derp.tools.shared_facts.db_propose_shared_fact",
            new=AsyncMock(return_value=proposal),
        ) as persist,
        capture_outbound_history(),
    ):
        result = await propose_shared_fact(ctx, "The release is Friday.")

    persist.assert_awaited_once_with(
        session,
        chat_id=UUID(int=1),
        thread_id=77,
        proposer_user_id=UUID(int=2),
        fact_text="The release is Friday.",
    )
    text = message.reply.await_args.args[0]
    markup = message.reply.await_args.kwargs["reply_markup"]
    assert text == (
        "<b>Save this fact?</b>\n"
        "<blockquote>The release is Friday.</blockquote>\n"
        "An admin must approve it before Derp can use it."
    )
    assert [button.text for button in markup.inline_keyboard[0]] == [
        "Approve",
        "Reject",
    ]
    callback = SharedFactCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert callback.fact_id == str(UUID(int=3))
    assert "Do not repeat" in result


def test_provider_exposes_only_proposals_to_the_model() -> None:
    provider = SharedFactTools()

    assert provider.get_tool(ChatTool.PROPOSE_SHARED_FACT) is not None
    assert provider.get_tool(ChatTool.REVIEW_SHARED_FACT) is None
    assert provider.get_tool(ChatTool.DELETE_SHARED_FACT) is None
