"""Tests for the AI response handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, User

from derp.handlers.ai_response import AIService, DerpMentionFilter


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


class TestAIService:
    """Test the AIService class."""

    def test_detailed_user_info_comprehensive(self):
        """Test comprehensive user info extraction."""
        service = AIService()

        user = MagicMock(spec=User)
        user.id = 12345
        user.username = "alice"
        user.full_name = "Alice Smith"
        user.language_code = "en"
        user.is_bot = False
        user.is_premium = True
        user.mention_markdown.return_value = "@alice"

        result = service._get_detailed_user_info(user)

        assert "Name: Alice Smith" in result
        assert "ID: 12345" in result
        assert "Language: en" in result
        assert "Bot: False" in result
        assert "@alice" in result

    def test_detailed_user_info_minimal(self):
        """Test user info with minimal data."""
        service = AIService()

        user = MagicMock(spec=User)
        user.id = 67890
        user.username = None
        user.full_name = None
        user.language_code = None
        user.is_bot = None
        user.is_premium = None
        user.mention_markdown.return_value = "@unknown"

        result = service._get_detailed_user_info(user)

        assert "ID: 67890" in result
        assert "Name: None" in result

    def test_detailed_chat_info_private(self):
        """Test chat info for private chat."""
        service = AIService()

        chat = MagicMock(spec=Chat)
        chat.id = -123456
        chat.type = "private"
        chat.full_name = "Alice Smith"
        chat.username = None

        result = service._get_detailed_chat_info(chat)

        assert "Type: private" in result
        assert "ID: -123456" in result
        assert "Title: Alice Smith" in result

    def test_detailed_chat_info_group(self):
        """Test chat info for group chat."""
        service = AIService()

        chat = MagicMock(spec=Chat)
        chat.id = -789012
        chat.type = "supergroup"
        chat.full_name = "Tech Discussion"
        chat.username = "tech_group"

        result = service._get_detailed_chat_info(chat)

        assert "Type: supergroup" in result
        assert "ID: -789012" in result
        assert "Title: Tech Discussion" in result
        assert "Username: @tech_group" in result

    def test_context_preparation_with_enhanced_info(self):
        """Test context preparation with enhanced user and chat information."""
        service = AIService()

        # Mock user with comprehensive info
        user = MagicMock(spec=User)
        user.id = 12345
        user.username = "bob"
        user.full_name = "Bob Jones"
        user.language_code = "en"
        user.is_bot = False
        user.is_premium = False
        user.mention_markdown.return_value = "@bob"

        # Mock chat
        chat = MagicMock(spec=Chat)
        chat.id = -67890
        chat.type = "private"
        chat.full_name = "Bob Jones"
        chat.username = None

        # Mock message
        message = MagicMock(spec=Message)
        message.text = "Hey derp, what's up?"
        message.from_user = user
        message.chat = chat
        message.reply_to_message = None
        message.message_id = 100
        message.date = MagicMock()
        message.date.isoformat.return_value = "2025-01-01T12:00:00"

        context = service.prepare_message_context(message)

        assert "Current user:" in context
        assert "@bob" in context
        assert "Name: Bob Jones" in context
        assert "Current chat:" in context
        assert "Type: private" in context
        assert "Message:" in context
        assert "Date:" in context

    def test_context_preparation_with_reply_enhanced(self):
        """Test context preparation with reply and enhanced information."""
        service = AIService()

        # Mock original user
        original_user = MagicMock(spec=User)
        original_user.id = 11111
        original_user.username = "alice"
        original_user.full_name = "Alice Smith"
        original_user.language_code = None
        original_user.is_bot = None
        original_user.is_premium = None
        original_user.mention_markdown.return_value = "@alice"

        # Mock original message
        original_message = MagicMock(spec=Message)
        original_message.text = "The sky is blue."
        original_message.caption = None
        original_message.from_user = original_user

        # Mock current user
        current_user = MagicMock(spec=User)
        current_user.id = 22222
        current_user.username = "bob"
        current_user.full_name = "Bob Jones"
        current_user.language_code = None
        current_user.is_bot = None
        current_user.is_premium = None
        current_user.mention_markdown.return_value = "@bob"

        # Mock chat
        chat = MagicMock(spec=Chat)
        chat.id = -33333
        chat.type = "group"
        chat.full_name = "Test Group"
        chat.username = None

        # Mock current message
        message = MagicMock(spec=Message)
        message.text = "derp, is that correct?"
        message.caption = None
        message.from_user = current_user
        message.chat = chat
        message.reply_to_message = original_message
        message.message_id = 200
        message.date = MagicMock()
        message.date.isoformat.return_value = "2025-01-01T12:05:00"

        context = service.prepare_message_context(message)

        assert "Current user:" in context
        assert "@bob" in context
        assert "Current chat:" in context
        assert "Type: group" in context
        assert "Title: Test Group" in context
        assert "Replied to user:" in context
        assert "@alice" in context
        assert 'Replied to message: ```"The sky is blue."```' in context
        assert "Message: ```derp, is that correct?```" in context

    @pytest.mark.asyncio
    async def test_generate_response_with_mock_agent(self):
        """Test AI response generation with mocked agent."""
        service = AIService()

        # Mock the agent and its result
        mock_result = MagicMock()
        mock_result.output = "This is a test response"

        mock_agent = AsyncMock()
        mock_agent.run.return_value = mock_result

        # Patch the agent property to return our mock
        with patch.object(service, "agent", mock_agent):
            response = await service.generate_response("Test context")

            assert response == "This is a test response"
            mock_agent.run.assert_called_once_with("Test context")

    def test_context_preparation_with_media_message(self):
        """Test context preparation when original message has media."""
        service = AIService()

        # Mock original user
        original_user = MagicMock(spec=User)
        original_user.id = 11111
        original_user.username = "alice"
        original_user.full_name = "Alice Jones"
        original_user.language_code = None
        original_user.is_bot = None
        original_user.is_premium = None
        original_user.mention_markdown.return_value = "@alice"

        # Mock original message with caption
        original_message = MagicMock(spec=Message)
        original_message.text = None  # No text
        original_message.caption = "Check out this photo!"
        original_message.from_user = original_user

        # Mock current user
        current_user = MagicMock(spec=User)
        current_user.id = 22222
        current_user.username = "bob"
        current_user.full_name = "Bob Smith"
        current_user.language_code = None
        current_user.is_bot = None
        current_user.is_premium = None
        current_user.mention_markdown.return_value = "@bob"

        # Mock chat
        chat = MagicMock(spec=Chat)
        chat.id = -33333
        chat.type = "private"
        chat.full_name = "Bob Smith"
        chat.username = None

        # Mock current message
        message = MagicMock(spec=Message)
        message.text = "derp, what do you think?"
        message.caption = None
        message.from_user = current_user
        message.chat = chat
        message.reply_to_message = original_message
        message.message_id = 300
        message.date = MagicMock()
        message.date.isoformat.return_value = "2025-01-01T12:10:00"

        context = service.prepare_message_context(message)

        assert "Replied to user:" in context
        assert "@alice" in context
        assert 'Replied to message: ```"Check out this photo!"```' in context
