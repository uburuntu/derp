"""Comprehensive tests for the MetaCommand filter.

The MetaCommand filter is a flexible filter that can match both slash commands
and hashtags, extract arguments, and provide metadata about the matched pattern.
"""

import pytest

from derp.filters.meta import MetaCommand, MetaInfo


class TestMetaCommandBasics:
    """Basic tests for MetaCommand filter functionality."""

    @pytest.mark.asyncio
    async def test_matches_simple_command(self, make_message):
        """Should match a simple command and extract all words as arguments."""
        message = make_message(text="/test hello world")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        assert result is not False
        assert isinstance(result, dict)
        assert "meta" in result
        meta = result["meta"]
        assert isinstance(meta, MetaInfo)
        assert meta.command == "test"
        # When args is None, all remaining words become arguments
        assert meta.arguments == ["hello", "world"]
        # Text contains everything after the command
        assert meta.text == "hello world"

    @pytest.mark.asyncio
    async def test_does_not_match_different_command(self, make_message):
        """Should not match if command is different."""
        message = make_message(text="/other hello")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        assert result is False

    @pytest.mark.asyncio
    async def test_matches_with_arguments(self, make_message):
        """Should extract command arguments correctly."""
        message = make_message(text="/imagine a beautiful sunset")
        filter_instance = MetaCommand("imagine")

        result = await filter_instance(message)

        meta = result["meta"]
        assert meta.command == "imagine"
        # Without args parameter, all words after command are arguments
        assert len(meta.arguments) >= 1
        assert meta.arguments[0] == "a"

    @pytest.mark.asyncio
    async def test_limited_arguments(self, make_message):
        """Should limit arguments when args parameter is set."""
        message = make_message(text="/edit style=cartoon fix the colors")
        filter_instance = MetaCommand("edit", args=1)

        result = await filter_instance(message)

        meta = result["meta"]
        assert meta.command == "edit"
        assert len(meta.arguments) == 1
        assert meta.arguments[0] == "style=cartoon"
        # Remaining text should be in .text
        assert "fix the colors" in meta.text


class TestMetaCommandWithMentions:
    """Tests for command matching with bot mentions."""

    @pytest.mark.asyncio
    async def test_matches_command_with_bot_mention(self, make_message):
        """Should match command with correct bot mention."""
        message = make_message(text="/start@DerpTestBot")
        filter_instance = MetaCommand("start")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.command == "start"

    @pytest.mark.asyncio
    async def test_rejects_command_with_wrong_bot_mention(self, make_message):
        """Should reject command with different bot mention."""
        message = make_message(text="/start@OtherBot")
        filter_instance = MetaCommand("start")

        result = await filter_instance(message)

        # Should be rejected because mention doesn't match our bot
        assert result is False

    @pytest.mark.asyncio
    async def test_command_without_mention_in_private_chat(self, make_message):
        """Commands without mentions should work in private chats."""
        message = make_message(
            text="/help",
            chat_type="private",
        )
        filter_instance = MetaCommand("help")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.command == "help"


class TestMetaCommandHashtags:
    """Tests for hashtag matching functionality."""

    @pytest.mark.asyncio
    async def test_matches_simple_hashtag(self, make_message):
        """Should match a simple hashtag."""
        message = make_message(text="This is #cool stuff")
        filter_instance = MetaCommand("cool")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.hashtag == "cool"
        assert meta.command is None
        # Text should be the message without the hashtag
        assert "This is" in meta.text
        assert "stuff" in meta.text
        assert "#cool" not in meta.text

    @pytest.mark.asyncio
    async def test_hashtag_with_arguments(self, make_message):
        """Should extract arguments from hashtag with underscores."""
        message = make_message(text="Check out #style_modern_minimal this design")
        filter_instance = MetaCommand("style")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.hashtag == "style"
        assert "modern" in meta.arguments
        assert "minimal" in meta.arguments

    @pytest.mark.asyncio
    async def test_hashtag_limited_arguments(self, make_message):
        """Should limit hashtag arguments when args parameter is set."""
        message = make_message(text="#filter_blur_10_50 the image")
        filter_instance = MetaCommand("filter", args=2)

        result = await filter_instance(message)

        meta = result["meta"]
        assert meta.hashtag == "filter"
        assert len(meta.arguments) == 2
        assert meta.arguments[0] == "blur"
        assert meta.arguments[1] == "10"
        # "50" should not be included due to args=2

    @pytest.mark.asyncio
    async def test_hashtag_not_at_word_boundary(self, make_message):
        """Should not match hashtag inside a word."""
        message = make_message(text="This is not#cool at all")
        filter_instance = MetaCommand("cool")

        result = await filter_instance(message)

        # Should not match because # is not at word boundary
        assert result is False


class TestMetaCommandCaseInsensitivity:
    """Tests for case-insensitive matching."""

    @pytest.mark.asyncio
    async def test_command_case_insensitive(self, make_message):
        """Commands should be case-insensitive by default."""
        message = make_message(text="/START hello")
        filter_instance = MetaCommand("start")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.command == "start"  # Should be normalized to lowercase

    @pytest.mark.asyncio
    async def test_hashtag_case_insensitive(self, make_message):
        """Hashtags should be case-insensitive by default."""
        message = make_message(text="This is #COOL stuff")
        filter_instance = MetaCommand("cool")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.hashtag is not None


class TestMetaCommandMultipleKeywords:
    """Tests for filters with multiple keywords."""

    @pytest.mark.asyncio
    async def test_matches_any_keyword(self, make_message):
        """Should match any of the provided keywords."""
        filter_instance = MetaCommand("start", "begin", "init")

        msg1 = make_message(text="/start")
        msg2 = make_message(text="/begin")
        msg3 = make_message(text="/init")

        result1 = await filter_instance(msg1)
        result2 = await filter_instance(msg2)
        result3 = await filter_instance(msg3)

        assert result1 is not False
        assert result2 is not False
        assert result3 is not False

    @pytest.mark.asyncio
    async def test_does_not_match_unlisted_keyword(self, make_message):
        """Should not match commands not in keyword list."""
        filter_instance = MetaCommand("start", "begin")
        message = make_message(text="/stop")

        result = await filter_instance(message)

        assert result is False


class TestMetaInfoProperties:
    """Tests for MetaInfo helper properties."""

    @pytest.mark.asyncio
    async def test_keyword_property_for_command(self, make_message):
        """keyword property should return command if present."""
        message = make_message(text="/test hello")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)
        meta = result["meta"]

        assert meta.keyword == "test"

    @pytest.mark.asyncio
    async def test_keyword_property_for_hashtag(self, make_message):
        """keyword property should return hashtag if present."""
        message = make_message(text="This is #test content")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)
        meta = result["meta"]

        assert meta.keyword == "test"

    @pytest.mark.asyncio
    async def test_target_message_with_text(self, make_message):
        """target_message should be the message itself if it has text."""
        message = make_message(text="/test hello world")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)
        meta = result["meta"]

        assert meta.target_message == message

    @pytest.mark.asyncio
    async def test_target_message_with_reply(self, make_message):
        """target_message should be reply_to_message if no text after command."""
        replied_to = make_message(text="Original message")
        message = make_message(text="/test", reply_to_message=replied_to)
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)
        meta = result["meta"]

        # If there's no text after command, target should be the replied message
        if not meta.text:
            assert meta.target_message == replied_to

    @pytest.mark.asyncio
    async def test_target_text_from_reply(self, make_message):
        """target_text should get text from reply_to_message if applicable."""
        replied_to = make_message(text="Please process this text")
        message = make_message(text="/process", reply_to_message=replied_to)
        filter_instance = MetaCommand("process")

        result = await filter_instance(message)
        meta = result["meta"]

        # target_text should include replied message text if no text after command
        if not meta.text:
            assert meta.target_text == "Please process this text"

    @pytest.mark.asyncio
    async def test_target_text_with_caption(self, make_message):
        """target_text should handle captions from replied messages."""
        replied_to = make_message(
            text=None,
            caption="Photo caption text",
            content_type="photo",
        )
        message = make_message(text="/analyze", reply_to_message=replied_to)
        filter_instance = MetaCommand("analyze")

        result = await filter_instance(message)
        meta = result["meta"]

        # Should get caption if no text
        if not meta.text and replied_to.caption:
            assert "caption" in meta.target_text.lower()


class TestMetaCommandWithCaptions:
    """Tests for command matching in photo/video captions."""

    @pytest.mark.asyncio
    async def test_matches_command_in_caption(self, make_message):
        """Should match commands in message captions."""
        message = make_message(
            text=None,
            caption="/describe what you see",
            content_type="photo",
        )
        filter_instance = MetaCommand("describe")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.command == "describe"


class TestMetaCommandEdgeCases:
    """Edge case tests for MetaCommand filter."""

    @pytest.mark.asyncio
    async def test_empty_message_returns_false(self, make_message):
        """Should return False for messages without text or caption."""
        message = make_message(text=None, caption=None)
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        assert result is False

    @pytest.mark.asyncio
    async def test_command_only_no_arguments(self, make_message):
        """Should handle command with no arguments or text."""
        message = make_message(text="/start")
        filter_instance = MetaCommand("start")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.command == "start"
        assert meta.arguments == []
        assert meta.text == ""

    @pytest.mark.asyncio
    async def test_multiple_hashtags_matches_first(self, make_message):
        """Should match the first occurrence of the hashtag."""
        message = make_message(text="First #test and second #test")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert meta.hashtag == "test"
        # Text should have first hashtag removed
        assert meta.text.count("#test") == 1  # Only one remaining

    @pytest.mark.asyncio
    async def test_special_characters_in_text(self, make_message):
        """Should handle special regex characters in text."""
        message = make_message(text="/test (parentheses) [brackets] {braces}")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert "(parentheses)" in meta.text
        assert "[brackets]" in meta.text

    @pytest.mark.asyncio
    async def test_unicode_in_arguments(self, make_message):
        """Should handle unicode characters in arguments."""
        message = make_message(text="/test привет мир 你好")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        assert result is not False
        meta = result["meta"]
        assert "привет" in meta.text or "привет" in meta.arguments


class TestMetaCommandIntegration:
    """Integration tests combining multiple features."""

    @pytest.mark.asyncio
    async def test_complex_hashtag_scenario(self, make_message):
        """Complex scenario with hashtag, arguments, and text."""
        message = make_message(
            text="Please apply #filter_sepia_80 to make it vintage"
        )
        filter_instance = MetaCommand("filter", args=2)

        result = await filter_instance(message)

        meta = result["meta"]
        assert meta.hashtag == "filter"
        assert meta.arguments == ["sepia", "80"]
        assert "Please apply" in meta.text
        assert "to make it vintage" in meta.text
        assert "#filter" not in meta.text

    @pytest.mark.asyncio
    async def test_command_with_bot_mention_and_args(self, make_message):
        """Command with bot mention and limited arguments."""
        message = make_message(text="/edit@DerpTestBot style modern fix colors")
        filter_instance = MetaCommand("edit", args=1)

        result = await filter_instance(message)

        meta = result["meta"]
        assert meta.command == "edit"
        assert len(meta.arguments) == 1
        assert meta.arguments[0] == "style"
        assert "modern fix colors" in meta.text

    @pytest.mark.asyncio
    async def test_prefers_command_over_hashtag(self, make_message):
        """Should match command if both command and hashtag are present."""
        message = make_message(text="/test some #test content")
        filter_instance = MetaCommand("test")

        result = await filter_instance(message)

        meta = result["meta"]
        # Command should take precedence
        assert meta.command == "test"
        assert meta.hashtag is None
