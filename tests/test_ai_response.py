"""Tests for the AI response handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, User

from derp.handlers.ai_response import AIService, DerpMentionFilter, PrivateChatFilter


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


class TestPrivateChatFilter:
    """Test the PrivateChatFilter."""

    @pytest.fixture
    def filter_instance(self):
        return PrivateChatFilter()

    @pytest.mark.asyncio
    async def test_detects_private_chat(self, filter_instance):
        """Test detection of private chat."""
        message = MagicMock(spec=Message)
        chat = MagicMock(spec=Chat)
        chat.type = "private"
        message.chat = chat

        result = await filter_instance(message)
        assert result is True

    @pytest.mark.asyncio
    async def test_rejects_group_chat(self, filter_instance):
        """Test rejection of group chat."""
        message = MagicMock(spec=Message)
        chat = MagicMock(spec=Chat)
        chat.type = "group"
        message.chat = chat

        result = await filter_instance(message)
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_supergroup_chat(self, filter_instance):
        """Test rejection of supergroup chat."""
        message = MagicMock(spec=Message)
        chat = MagicMock(spec=Chat)
        chat.type = "supergroup"
        message.chat = chat

        result = await filter_instance(message)
        assert result is False


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

        result = service._get_detailed_user_info(user)

        assert "@alice" in result
        assert "Name: Alice Smith" in result
        assert "ID: 12345" in result
        assert "Language: en" in result
        assert "Bot: False" in result
        assert "Premium: True" in result

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

        result = service._get_detailed_user_info(user)

        assert "ID: 67890" in result
        assert "@" not in result  # No username
        assert "Name:" not in result  # No name

    def test_detailed_chat_info_private(self):
        """Test chat info for private chat."""
        service = AIService()

        chat = MagicMock(spec=Chat)
        chat.id = -123456
        chat.type = "private"
        chat.title = None
        chat.username = None
        chat.description = None
        chat.member_count = None

        result = service._get_detailed_chat_info(chat)

        assert "Type: private" in result
        assert "ID: -123456" in result

    def test_detailed_chat_info_group(self):
        """Test chat info for group chat."""
        service = AIService()

        chat = MagicMock(spec=Chat)
        chat.id = -789012
        chat.type = "supergroup"
        chat.title = "Tech Discussion"
        chat.username = "tech_group"
        chat.description = "A group for tech discussions"
        chat.member_count = 150

        result = service._get_detailed_chat_info(chat)

        assert "Type: supergroup" in result
        assert "ID: -789012" in result
        assert "Title: Tech Discussion" in result
        assert "Username: @tech_group" in result
        assert "Description: A group for tech discussions" in result
        assert "Members: 150" in result

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

        # Mock chat
        chat = MagicMock(spec=Chat)
        chat.id = -67890
        chat.type = "private"
        chat.title = None
        chat.username = None
        chat.description = None
        chat.member_count = None

        # Mock message
        message = MagicMock(spec=Message)
        message.text = "Hey derp, what's up?"
        message.from_user = user
        message.chat = chat
        message.reply_to_message = None
        message.message_id = 100
        message.date = "2025-01-01 12:00:00"

        context = service.prepare_message_context(message)

        assert "User Information:" in context
        assert "@bob" in context
        assert "Name: Bob Jones" in context
        assert "Chat Information:" in context
        assert "Type: private" in context
        assert "Current Message:" in context
        assert "Message ID: 100" in context
        assert "Message Date:" in context

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

        # Mock original message
        original_message = MagicMock(spec=Message)
        original_message.text = "The sky is blue."
        original_message.from_user = original_user

        # Mock current user
        current_user = MagicMock(spec=User)
        current_user.id = 22222
        current_user.username = "bob"
        current_user.full_name = "Bob Jones"
        current_user.language_code = None
        current_user.is_bot = None
        current_user.is_premium = None

        # Mock chat
        chat = MagicMock(spec=Chat)
        chat.id = -33333
        chat.type = "group"
        chat.title = "Test Group"
        chat.username = None
        chat.description = None
        chat.member_count = None

        # Mock current message
        message = MagicMock(spec=Message)
        message.text = "derp, is that correct?"
        message.from_user = current_user
        message.chat = chat
        message.reply_to_message = original_message
        message.message_id = 200
        message.date = "2025-01-01 12:05:00"

        context = service.prepare_message_context(message)

        assert "User Information:" in context
        assert "@bob" in context
        assert "Chat Information:" in context
        assert "Type: group" in context
        assert "Title: Test Group" in context
        assert "Replied Message Context:" in context
        assert "Original Author:" in context
        assert "@alice" in context
        assert 'Original Message: "The sky is blue."' in context
        assert 'Current Message: "derp, is that correct?"' in context

    @pytest.mark.asyncio
    async def test_generate_response_with_mock_agent(self):
        """Test AI response generation with mocked agent."""
        service = AIService()

        # Mock the agent and its result
        mock_result = MagicMock()
        mock_result.output = "This is a test response"

        mock_agent = AsyncMock()
        mock_agent.run.return_value = mock_result

        # Patch the _get_agent method to return our mock
        with patch.object(service, "_get_agent", return_value=mock_agent):
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

        # Mock chat
        chat = MagicMock(spec=Chat)
        chat.id = -33333
        chat.type = "private"
        chat.title = None
        chat.username = None
        chat.description = None
        chat.member_count = None

        # Mock current message
        message = MagicMock(spec=Message)
        message.text = "derp, what do you think?"
        message.from_user = current_user
        message.chat = chat
        message.reply_to_message = original_message
        message.message_id = 300
        message.date = "2025-01-01 12:10:00"

        context = service.prepare_message_context(message)

        assert "Original Author:" in context
        assert "@alice" in context
        assert 'Original Message: "Check out this photo!" (with media)' in context
