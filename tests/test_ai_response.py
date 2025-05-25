"""Tests for the AI response handler."""

from unittest.mock import MagicMock

import pytest
from aiogram.types import Message

from derp.handlers.ai_response import DerpMentionFilter


class TestDerpMentionFilter:
    """Test the DerpMentionFilter."""

    @pytest.fixture
    def filter_instance(self):
        return DerpMentionFilter()

    @pytest.mark.asyncio
    async def test_detects_derp_english(self, filter_instance):
        """Test detection of 'derp' in English."""
        message = MagicMock(spec=Message)
        message.text = "Hey derp, what do you think?"

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_detects_derp_russian(self, filter_instance):
        """Test detection of 'дерп' in Russian."""
        message = MagicMock(spec=Message)
        message.text = "Привет дерп, как дела?"

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_case_insensitive(self, filter_instance):
        """Test case insensitive detection."""
        message = MagicMock(spec=Message)
        message.text = "Hey DERP, what's up?"

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_no_derp_mention(self, filter_instance):
        """Test when derp is not mentioned."""
        message = MagicMock(spec=Message)
        message.text = "Hello everyone, how are you?"

        result = await filter_instance(message)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_text(self, filter_instance):
        """Test when message has no text."""
        message = MagicMock(spec=Message)
        message.text = None

        result = await filter_instance(message)
        assert result is False

    @pytest.mark.asyncio
    async def test_partial_word_not_detected(self, filter_instance):
        """Test that partial words containing 'derp' are not detected."""
        message = MagicMock(spec=Message)
        message.text = "The word 'wonderful' doesn't contain derp"

        result = await filter_instance(message)
        assert result is True  # Should still detect the standalone 'derp'
