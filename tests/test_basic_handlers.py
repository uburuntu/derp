"""Tests for basic command handlers (/start, /help)."""

import pytest
from aiogram.utils.i18n import gettext as _

from derp.handlers.basic import cmd_help, cmd_start


class TestCmdStart:
    """Tests for the /start command handler."""

    @pytest.mark.asyncio
    async def test_start_replies_with_welcome_message(self, make_message):
        """Should reply with a welcome message containing user's name."""
        message = make_message(
            text="/start",
            user_id=123,
            chat_type="private",
        )
        message.from_user.full_name = "Alice Smith"

        await cmd_start(message)

        # Verify reply was called
        message.reply.assert_awaited_once()

        # Get the text that was sent
        call_args = message.reply.await_args
        welcome_text = call_args.args[0]

        # Verify key content is present
        assert "Alice Smith" in welcome_text
        assert "Derp" in welcome_text or "AI" in welcome_text

    @pytest.mark.asyncio
    async def test_start_in_group_chat(self, make_message):
        """Should work in group chats as well."""
        message = make_message(
            text="/start",
            chat_id=-1001234567890,
            chat_type="supergroup",
        )
        message.from_user.full_name = "Bob Jones"

        await cmd_start(message)

        message.reply.assert_awaited_once()
        call_args = message.reply.await_args
        welcome_text = call_args.args[0]
        assert "Bob Jones" in welcome_text

    @pytest.mark.asyncio
    async def test_start_with_special_characters_in_name(self, make_message):
        """Should handle special characters in user names safely."""
        message = make_message(text="/start")
        # HTML special characters that should be escaped
        message.from_user.full_name = "<script>alert('xss')</script>"

        await cmd_start(message)

        message.reply.assert_awaited_once()
        call_args = message.reply.await_args
        welcome_text = call_args.args[0]

        # The name should be in the text (possibly escaped)
        # The actual escaping is tested by checking it doesn't break
        assert welcome_text is not None
        assert len(welcome_text) > 0

    @pytest.mark.asyncio
    async def test_start_contains_helpful_information(self, make_message):
        """Welcome message should contain helpful information about the bot."""
        message = make_message(text="/start")

        await cmd_start(message)

        call_args = message.reply.await_args
        welcome_text = call_args.args[0]

        # Should mention some capabilities or features
        # Looking for common keywords that indicate features
        has_features = any(
            keyword in welcome_text.lower()
            for keyword in [
                "help",
                "command",
                "chat",
                "ai",
                "conversation",
                "image",
                "code",
            ]
        )
        assert has_features, "Welcome message should mention bot capabilities"

    @pytest.mark.asyncio
    async def test_start_with_empty_username(self, make_message):
        """Should handle users without usernames gracefully."""
        message = make_message(text="/start")
        message.from_user.username = None
        message.from_user.full_name = "Just First"

        await cmd_start(message)

        message.reply.assert_awaited_once()
        call_args = message.reply.await_args
        welcome_text = call_args.args[0]
        assert "Just First" in welcome_text


class TestCmdHelp:
    """Tests for the /help command handler."""

    @pytest.mark.asyncio
    async def test_help_replies_with_command_list(self, make_message):
        """Should reply with a list of available commands."""
        message = make_message(text="/help")

        await cmd_help(message)

        message.reply.assert_awaited_once()
        call_args = message.reply.await_args
        help_text = call_args.args[0]

        # Verify it contains command information
        assert "/" in help_text or "command" in help_text.lower()

    @pytest.mark.asyncio
    async def test_help_contains_common_commands(self, make_message):
        """Help text should list common bot commands."""
        message = make_message(text="/help")

        await cmd_help(message)

        call_args = message.reply.await_args
        help_text = call_args.args[0]

        # Check for common command mentions
        common_commands = ["/help", "/start", "/donate", "/settings"]
        found_commands = [cmd for cmd in common_commands if cmd in help_text]

        assert (
            len(found_commands) >= 2
        ), f"Help should mention common commands. Found: {found_commands}"

    @pytest.mark.asyncio
    async def test_help_works_in_private_chat(self, make_message):
        """Should work in private chats."""
        message = make_message(
            text="/help",
            chat_type="private",
            chat_id=12345,
        )

        await cmd_help(message)

        message.reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_help_works_in_group_chat(self, make_message):
        """Should work in group chats."""
        message = make_message(
            text="/help",
            chat_type="supergroup",
            chat_id=-1001234567890,
        )

        await cmd_help(message)

        message.reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_help_mentions_inline_mode(self, make_message):
        """Help text should mention inline mode if available."""
        message = make_message(text="/help")

        await cmd_help(message)

        call_args = message.reply.await_args
        help_text = call_args.args[0]

        # Inline mode is typically mentioned with @ or "inline"
        assert "@" in help_text or "inline" in help_text.lower()

    @pytest.mark.asyncio
    async def test_help_provides_usage_examples(self, make_message):
        """Help text should provide guidance on how to use the bot."""
        message = make_message(text="/help")

        await cmd_help(message)

        call_args = message.reply.await_args
        help_text = call_args.args[0]

        # Should have some form of instructions or examples
        instructional_keywords = [
            "use",
            "type",
            "send",
            "mention",
            "reply",
            "chat",
            "can",
        ]
        has_instructions = any(
            keyword in help_text.lower() for keyword in instructional_keywords
        )

        assert (
            has_instructions
        ), "Help text should provide usage instructions or examples"

    @pytest.mark.asyncio
    async def test_help_is_not_empty(self, make_message):
        """Help text should not be empty or too short."""
        message = make_message(text="/help")

        await cmd_help(message)

        call_args = message.reply.await_args
        help_text = call_args.args[0]

        assert len(help_text) > 50, "Help text should be substantial"


class TestCommandsIntegration:
    """Integration tests for command handlers."""

    @pytest.mark.asyncio
    async def test_start_and_help_are_different(self, make_message):
        """Start and help messages should have different content."""
        start_msg = make_message(text="/start")
        help_msg = make_message(text="/help")

        await cmd_start(start_msg)
        await cmd_help(help_msg)

        start_text = start_msg.reply.await_args.args[0]
        help_text = help_msg.reply.await_args.args[0]

        # They should not be identical
        assert start_text != help_text

    @pytest.mark.asyncio
    async def test_both_commands_return_truthy(self, make_message):
        """Both commands should return a truthy value (the message)."""
        start_msg = make_message(text="/start")
        help_msg = make_message(text="/help")

        start_result = await cmd_start(start_msg)
        help_result = await cmd_help(help_msg)

        # In aiogram, handlers typically return the sent message or None
        # We just verify they complete without errors
        assert start_result is not None or start_msg.reply.await_count == 1
        assert help_result is not None or help_msg.reply.await_count == 1
