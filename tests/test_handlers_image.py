"""Tests for image generation handler."""

from unittest.mock import MagicMock

import pytest

from derp.credits.models import ModelTier
from derp.handlers.image import (
    handle_edit,
    handle_imagine,
)


class TestHandleImagine:
    """Tests for /imagine command handler."""

    @pytest.mark.asyncio
    async def test_missing_prompt(
        self, make_message, mock_credit_service_factory, mock_sender
    ):
        """Test /imagine without prompt shows usage."""
        message = make_message(text="/imagine")

        meta = MagicMock()
        meta.target_text = ""
        service = mock_credit_service_factory()
        sender = mock_sender(message)

        await handle_imagine(message, meta, sender, service, None, None)

        message.reply.assert_awaited_once()
        assert "Usage" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_user(
        self, make_message, mock_credit_service_factory, mock_sender
    ):
        """Test /imagine without user shows error."""
        message = make_message(text="/imagine a cat")

        meta = MagicMock()
        meta.target_text = "a cat"
        service = mock_credit_service_factory()
        sender = mock_sender(message)

        # user_model=None, chat_model provided
        await handle_imagine(message, meta, sender, service, None, MagicMock())

        message.reply.assert_awaited_once()
        assert "Could not verify" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_credits(
        self,
        make_message,
        mock_user_model,
        mock_chat_model,
        mock_credit_service_factory,
        make_credit_check_result,
        mock_sender,
    ):
        """Test /imagine rejected when no credits."""
        message = make_message(text="/imagine a cat")

        meta = MagicMock()
        meta.target_text = "a cat"

        user = mock_user_model()
        chat = mock_chat_model()
        sender = mock_sender(message)

        check_result = make_credit_check_result(
            allowed=False,
            tier=ModelTier.STANDARD,
            model_id="gemini-2.5-flash-image",
            reject_reason="Not enough credits",
        )
        service = mock_credit_service_factory(check_result=check_result)

        await handle_imagine(
            message, meta, sender, service, user_model=user, chat_model=chat
        )

        message.reply.assert_awaited_once()
        assert "Not enough credits" in message.reply.call_args[0][0]


class TestHandleEdit:
    """Tests for /edit command handler."""

    @pytest.mark.asyncio
    async def test_missing_prompt(
        self, make_message, mock_credit_service_factory, mock_sender
    ):
        """Test /edit without prompt shows usage."""
        message = make_message(text="/edit")

        meta = MagicMock()
        meta.target_text = ""
        service = mock_credit_service_factory()
        sender = mock_sender(message)

        await handle_edit(message, meta, sender, service, None, None)

        message.reply.assert_awaited_once()
        assert "Reply to an image" in message.reply.call_args[0][0]
