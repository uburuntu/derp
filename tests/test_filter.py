"""Tests for the DerpMentionFilter."""

from unittest.mock import MagicMock

import pytest
from aiogram.types import Message

from derp.filters import DerpMentionFilter


class TestDerpMentionFilter:
    """Unit tests for detecting 'derp' or 'дерп' in messages."""

    @pytest.fixture
    def filter_instance(self):
        return DerpMentionFilter()

    @pytest.mark.asyncio
    async def test_detects_derp_english(self, filter_instance):
        """Detects 'derp' in English text."""
        message = MagicMock(spec=Message)
        message.text = "Hey derp, what do you think?"

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_detects_derp_russian(self, filter_instance):
        """Detects 'дерп' in Russian text."""
        message = MagicMock(spec=Message)
        message.text = "Привет дерп, как дела?"

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_case_insensitive(self, filter_instance):
        """Detects regardless of case (uppercase/lowercase)."""
        message = MagicMock(spec=Message)
        message.text = "Hey DERP, what's up?"

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_no_derp_mention(self, filter_instance):
        """Returns False if 'derp' is not mentioned."""
        message = MagicMock(spec=Message)
        message.text = "Hello everyone, how are you?"

        result = await filter_instance(message)
        assert result is False

    @pytest.mark.asyncio
    async def test_word_with_derp_inside(self, filter_instance):
        """Detects when 'derp' appears as a separate word, but not hidden inside others."""
        message = MagicMock(spec=Message)
        message.text = "That guy is such a derpish character."

        result = await filter_instance(message)
        assert result is False
