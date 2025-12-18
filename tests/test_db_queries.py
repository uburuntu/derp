"""Comprehensive tests for database query functions.

These tests use real PostgreSQL to ensure queries work correctly,
handle edge cases, and perform as expected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from derp.db.queries import (
    get_chat_by_telegram_id,
    get_chat_settings,
    get_recent_messages,
    get_user_by_telegram_id,
    mark_message_deleted,
    update_chat_memory,
    upsert_chat,
    upsert_message,
    upsert_user,
)
from derp.models import Message


class TestUserQueries:
    """Tests for user-related database queries."""

    @pytest.mark.asyncio
    async def test_upsert_user_creates_new_user(self, db_session):
        """Should create a new user when telegram_id doesn't exist."""
        user = await upsert_user(
            db_session,
            telegram_id=111111,
            is_bot=False,
            first_name="Alice",
            last_name="Smith",
            username="alice",
            language_code="en",
            is_premium=True,
        )

        assert user.id is not None
        assert user.telegram_id == 111111
        assert user.first_name == "Alice"
        assert user.last_name == "Smith"
        assert user.username == "alice"
        assert user.is_premium is True

    @pytest.mark.asyncio
    async def test_upsert_user_updates_existing_user(self, db_session):
        """Should update existing user when telegram_id already exists."""
        # Create initial user
        user1 = await upsert_user(
            db_session,
            telegram_id=222222,
            is_bot=False,
            first_name="Bob",
            last_name="Old",
            username="bob_old",
        )
        original_id = user1.id

        # Upsert with updated data
        user2 = await upsert_user(
            db_session,
            telegram_id=222222,
            is_bot=False,
            first_name="Bob",
            last_name="New",
            username="bob_new",
            is_premium=True,
        )

        assert user2.id == original_id  # Same record
        assert user2.last_name == "New"
        assert user2.username == "bob_new"
        assert user2.is_premium is True

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_found(self, db_session):
        """Should return user when telegram_id exists."""
        await upsert_user(
            db_session,
            telegram_id=333333,
            is_bot=False,
            first_name="Charlie",
        )

        user = await get_user_by_telegram_id(db_session, 333333)

        assert user is not None
        assert user.telegram_id == 333333
        assert user.first_name == "Charlie"

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_not_found(self, db_session):
        """Should return None when telegram_id doesn't exist."""
        user = await get_user_by_telegram_id(db_session, 999999999)
        assert user is None

    @pytest.mark.asyncio
    async def test_user_computed_properties(self, db_session):
        """Should have correct computed properties."""
        # User with last name
        user1 = await upsert_user(
            db_session,
            telegram_id=444444,
            is_bot=False,
            first_name="David",
            last_name="Wilson",
            username="david",
        )
        assert user1.full_name == "David Wilson"
        assert user1.display_name == "@david"

        # User without username
        user2 = await upsert_user(
            db_session,
            telegram_id=555555,
            is_bot=False,
            first_name="Eve",
            last_name=None,
            username=None,
        )
        assert user2.full_name == "Eve"
        assert user2.display_name == "Eve"


class TestChatQueries:
    """Tests for chat-related database queries."""

    @pytest.mark.asyncio
    async def test_upsert_chat_creates_new_chat(self, db_session):
        """Should create a new chat when telegram_id doesn't exist."""
        chat = await upsert_chat(
            db_session,
            telegram_id=-1001111111111,
            chat_type="supergroup",
            title="Test Group",
            username="testgroup",
            is_forum=True,
        )

        assert chat.id is not None
        assert chat.telegram_id == -1001111111111
        assert chat.type == "supergroup"
        assert chat.title == "Test Group"
        assert chat.is_forum is True

    @pytest.mark.asyncio
    async def test_upsert_chat_updates_existing_chat(self, db_session):
        """Should update existing chat when telegram_id already exists."""
        # Create initial chat
        chat1 = await upsert_chat(
            db_session,
            telegram_id=-1002222222222,
            chat_type="group",
            title="Old Title",
        )
        original_id = chat1.id

        # Upsert with updated data
        chat2 = await upsert_chat(
            db_session,
            telegram_id=-1002222222222,
            chat_type="supergroup",  # Upgraded
            title="New Title",
            username="newusername",
        )

        assert chat2.id == original_id
        assert chat2.type == "supergroup"
        assert chat2.title == "New Title"
        assert chat2.username == "newusername"

    @pytest.mark.asyncio
    async def test_get_chat_by_telegram_id(self, db_session):
        """Should return chat when telegram_id exists."""
        await upsert_chat(
            db_session,
            telegram_id=-1003333333333,
            chat_type="private",
            first_name="Private",
            last_name="User",
        )

        chat = await get_chat_by_telegram_id(db_session, -1003333333333)

        assert chat is not None
        assert chat.type == "private"
        assert chat.first_name == "Private"

    @pytest.mark.asyncio
    async def test_chat_display_name_variants(self, db_session):
        """Should compute display_name correctly for different chat types."""
        # Group with title
        group = await upsert_chat(
            db_session,
            telegram_id=-1004444444444,
            chat_type="supergroup",
            title="My Group",
        )
        assert group.display_name == "My Group"

        # Private chat with username
        private_with_username = await upsert_chat(
            db_session,
            telegram_id=55555555,
            chat_type="private",
            first_name="John",
            username="johndoe",
        )
        assert private_with_username.display_name == "@johndoe"

        # Private chat without username
        private_no_username = await upsert_chat(
            db_session,
            telegram_id=66666666,
            chat_type="private",
            first_name="Jane",
            last_name="Doe",
        )
        assert private_no_username.display_name == "Jane Doe"


class TestChatMemoryQueries:
    """Tests for chat memory (LLM context) operations."""

    @pytest.mark.asyncio
    async def test_update_chat_memory_sets_memory(self, db_session):
        """Should set llm_memory for a chat."""
        await upsert_chat(
            db_session,
            telegram_id=-1005555555555,
            chat_type="supergroup",
            title="Memory Test",
        )

        await update_chat_memory(
            db_session,
            telegram_id=-1005555555555,
            llm_memory="Remember: User prefers Python over JavaScript",
        )

        chat = await get_chat_settings(db_session, -1005555555555)
        assert chat is not None
        assert chat.llm_memory == "Remember: User prefers Python over JavaScript"

    @pytest.mark.asyncio
    async def test_update_chat_memory_clears_memory(self, db_session):
        """Should clear llm_memory when set to None."""
        await upsert_chat(
            db_session,
            telegram_id=-1006666666666,
            chat_type="supergroup",
            title="Clear Test",
        )

        # Set memory
        await update_chat_memory(
            db_session, telegram_id=-1006666666666, llm_memory="Some memory"
        )

        # Clear memory
        await update_chat_memory(
            db_session, telegram_id=-1006666666666, llm_memory=None
        )

        chat = await get_chat_settings(db_session, -1006666666666)
        assert chat is not None
        assert chat.llm_memory is None

    @pytest.mark.asyncio
    async def test_get_chat_settings_returns_memory(self, db_session):
        """Should return chat with llm_memory field."""
        await upsert_chat(
            db_session,
            telegram_id=-1007777777777,
            chat_type="supergroup",
            title="Settings Test",
        )
        await update_chat_memory(
            db_session, telegram_id=-1007777777777, llm_memory="Context: coding help"
        )

        settings = await get_chat_settings(db_session, -1007777777777)

        assert settings is not None
        assert settings.llm_memory == "Context: coding help"


class TestMessageQueries:
    """Tests for message-related database queries."""

    @pytest.mark.asyncio
    async def test_upsert_message_creates_new_message(self, db_session):
        """Should create a new message."""
        # First create chat and user
        await upsert_chat(
            db_session,
            telegram_id=-1008888888888,
            chat_type="supergroup",
            title="Message Test",
        )
        await upsert_user(
            db_session,
            telegram_id=88888888,
            is_bot=False,
            first_name="Sender",
        )

        message = await upsert_message(
            db_session,
            chat_telegram_id=-1008888888888,
            user_telegram_id=88888888,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="Hello world!",
            telegram_date=datetime.now(UTC),
        )

        assert message is not None
        assert message.telegram_message_id == 1
        assert message.text == "Hello world!"
        assert message.direction == "in"

    @pytest.mark.asyncio
    async def test_upsert_message_updates_existing(self, db_session):
        """Should update message when natural key exists."""
        await upsert_chat(
            db_session,
            telegram_id=-1009999999999,
            chat_type="supergroup",
            title="Edit Test",
        )
        await upsert_user(
            db_session,
            telegram_id=99999999,
            is_bot=False,
            first_name="Editor",
        )

        # Create message
        msg1 = await upsert_message(
            db_session,
            chat_telegram_id=-1009999999999,
            user_telegram_id=99999999,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="Original text",
            telegram_date=datetime.now(UTC),
        )

        # Update same message (edit)
        msg2 = await upsert_message(
            db_session,
            chat_telegram_id=-1009999999999,
            user_telegram_id=99999999,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="Edited text",
            telegram_date=datetime.now(UTC),
            edited_at=datetime.now(UTC),
        )

        assert msg2.id == msg1.id  # Same record
        assert msg2.text == "Edited text"
        assert msg2.edited_at is not None

    @pytest.mark.asyncio
    async def test_upsert_message_without_user(self, db_session):
        """Should create message even without a user (channel posts)."""
        await upsert_chat(
            db_session,
            telegram_id=-1001010101010,
            chat_type="channel",
            title="Channel",
        )

        message = await upsert_message(
            db_session,
            chat_telegram_id=-1001010101010,
            user_telegram_id=None,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="Channel post",
            telegram_date=datetime.now(UTC),
        )

        assert message is not None
        assert message.user_id is None
        assert message.text == "Channel post"

    @pytest.mark.asyncio
    async def test_mark_message_deleted(self, db_session):
        """Should set deleted_at timestamp on message."""
        await upsert_chat(
            db_session,
            telegram_id=-1001111111111,
            chat_type="supergroup",
            title="Delete Test",
        )
        await upsert_user(
            db_session, telegram_id=11111111, is_bot=False, first_name="Deleter"
        )

        await upsert_message(
            db_session,
            chat_telegram_id=-1001111111111,
            user_telegram_id=11111111,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="To be deleted",
            telegram_date=datetime.now(UTC),
        )

        deleted_at = datetime.now(UTC)
        await mark_message_deleted(
            db_session,
            chat_telegram_id=-1001111111111,
            telegram_message_id=1,
            deleted_at=deleted_at,
        )

        # Verify via direct query
        chat = await get_chat_by_telegram_id(db_session, -1001111111111)
        stmt = select(Message).where(
            Message.chat_id == chat.id, Message.telegram_message_id == 1
        )
        result = await db_session.execute(stmt)
        message = result.scalar_one()

        assert message.deleted_at is not None
        assert message.is_deleted is True


class TestGetRecentMessages:
    """Tests for the get_recent_messages query (critical for LLM context)."""

    @pytest.mark.asyncio
    async def test_returns_messages_in_chronological_order(self, db_session):
        """Should return messages oldest-first for proper context building."""
        await upsert_chat(
            db_session,
            telegram_id=-1001212121212,
            chat_type="supergroup",
            title="Order Test",
        )
        await upsert_user(
            db_session, telegram_id=12121212, is_bot=False, first_name="Orderer"
        )

        # Create messages with different timestamps
        base_time = datetime.now(UTC)
        for i in range(5):
            await upsert_message(
                db_session,
                chat_telegram_id=-1001212121212,
                user_telegram_id=12121212,
                telegram_message_id=i + 1,
                thread_id=None,
                direction="in",
                content_type="text",
                text=f"Message {i + 1}",
                telegram_date=base_time + timedelta(seconds=i),
            )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001212121212, limit=10
        )

        assert len(messages) == 5
        # Should be oldest first
        assert messages[0].text == "Message 1"
        assert messages[4].text == "Message 5"

    @pytest.mark.asyncio
    async def test_excludes_deleted_messages(self, db_session):
        """Should not return messages that have been deleted."""
        await upsert_chat(
            db_session,
            telegram_id=-1001313131313,
            chat_type="supergroup",
            title="Delete Excl",
        )
        await upsert_user(
            db_session, telegram_id=13131313, is_bot=False, first_name="ExcludeTest"
        )

        # Create messages
        for i in range(3):
            await upsert_message(
                db_session,
                chat_telegram_id=-1001313131313,
                user_telegram_id=13131313,
                telegram_message_id=i + 1,
                thread_id=None,
                direction="in",
                content_type="text",
                text=f"Message {i + 1}",
                telegram_date=datetime.now(UTC),
            )

        # Delete the second message
        await mark_message_deleted(
            db_session,
            chat_telegram_id=-1001313131313,
            telegram_message_id=2,
        )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001313131313, limit=10
        )

        assert len(messages) == 2
        texts = [m.text for m in messages]
        assert "Message 2" not in texts

    @pytest.mark.asyncio
    async def test_respects_limit(self, db_session):
        """Should return at most 'limit' messages."""
        await upsert_chat(
            db_session,
            telegram_id=-1001414141414,
            chat_type="supergroup",
            title="Limit Test",
        )
        await upsert_user(
            db_session, telegram_id=14141414, is_bot=False, first_name="Limiter"
        )

        # Create 10 messages
        for i in range(10):
            await upsert_message(
                db_session,
                chat_telegram_id=-1001414141414,
                user_telegram_id=14141414,
                telegram_message_id=i + 1,
                thread_id=None,
                direction="in",
                content_type="text",
                text=f"Message {i + 1}",
                telegram_date=datetime.now(UTC),
            )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001414141414, limit=5
        )

        assert len(messages) == 5
        # Should be the 5 most recent, in chronological order
        assert messages[0].text == "Message 6"
        assert messages[4].text == "Message 10"

    @pytest.mark.asyncio
    async def test_includes_user_relationship(self, db_session):
        """Should eagerly load user for each message."""
        await upsert_chat(
            db_session,
            telegram_id=-1001515151515,
            chat_type="supergroup",
            title="Eager Test",
        )
        await upsert_user(
            db_session,
            telegram_id=15151515,
            is_bot=False,
            first_name="Eager",
            username="eagerman",
        )

        await upsert_message(
            db_session,
            chat_telegram_id=-1001515151515,
            user_telegram_id=15151515,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="Test message",
            telegram_date=datetime.now(UTC),
        )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001515151515, limit=10
        )

        assert len(messages) == 1
        assert messages[0].user is not None
        assert messages[0].user.username == "eagerman"
        assert messages[0].user.display_name == "@eagerman"

    @pytest.mark.asyncio
    async def test_returns_empty_for_nonexistent_chat(self, db_session):
        """Should return empty list for chat that doesn't exist."""
        messages = await get_recent_messages(
            db_session, chat_telegram_id=-999999999999, limit=10
        )

        assert messages == []

    @pytest.mark.asyncio
    async def test_handles_both_directions(self, db_session):
        """Should return both inbound and outbound messages."""
        await upsert_chat(
            db_session,
            telegram_id=-1001616161616,
            chat_type="supergroup",
            title="Direction Test",
        )
        await upsert_user(
            db_session, telegram_id=16161616, is_bot=False, first_name="Human"
        )
        await upsert_user(
            db_session, telegram_id=16161617, is_bot=True, first_name="Bot"
        )

        # Inbound message
        await upsert_message(
            db_session,
            chat_telegram_id=-1001616161616,
            user_telegram_id=16161616,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="text",
            text="User question",
            telegram_date=datetime.now(UTC),
        )

        # Outbound message (bot response)
        await upsert_message(
            db_session,
            chat_telegram_id=-1001616161616,
            user_telegram_id=16161617,
            telegram_message_id=2,
            thread_id=None,
            direction="out",
            content_type="text",
            text="Bot answer",
            telegram_date=datetime.now(UTC),
        )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001616161616, limit=10
        )

        assert len(messages) == 2
        assert any(m.direction == "in" for m in messages)
        assert any(m.direction == "out" for m in messages)


class TestMessageWithAttachments:
    """Tests for messages with media attachments."""

    @pytest.mark.asyncio
    async def test_message_with_photo(self, db_session):
        """Should store photo attachment metadata."""
        await upsert_chat(
            db_session,
            telegram_id=-1001717171717,
            chat_type="supergroup",
            title="Photo Test",
        )
        await upsert_user(
            db_session, telegram_id=17171717, is_bot=False, first_name="Photographer"
        )

        message = await upsert_message(
            db_session,
            chat_telegram_id=-1001717171717,
            user_telegram_id=17171717,
            telegram_message_id=1,
            thread_id=None,
            direction="in",
            content_type="photo",
            text="Check out this photo!",
            attachment_type="photo",
            attachment_file_id="AgACAgIAAx0FAKE_PHOTO_ID",
            telegram_date=datetime.now(UTC),
        )

        assert message.content_type == "photo"
        assert message.attachment_type == "photo"
        assert message.attachment_file_id == "AgACAgIAAx0FAKE_PHOTO_ID"

    @pytest.mark.asyncio
    async def test_message_with_media_group(self, db_session):
        """Should store media_group_id for album messages."""
        await upsert_chat(
            db_session,
            telegram_id=-1001818181818,
            chat_type="supergroup",
            title="Album Test",
        )
        await upsert_user(
            db_session, telegram_id=18181818, is_bot=False, first_name="AlbumMaker"
        )

        media_group_id = "1234567890"

        # Create album with multiple photos
        for i in range(3):
            await upsert_message(
                db_session,
                chat_telegram_id=-1001818181818,
                user_telegram_id=18181818,
                telegram_message_id=i + 1,
                thread_id=None,
                direction="in",
                content_type="photo",
                text=None,  # Albums typically have caption only on first
                media_group_id=media_group_id,
                attachment_type="photo",
                attachment_file_id=f"photo_{i}",
                telegram_date=datetime.now(UTC),
            )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001818181818, limit=10
        )

        assert len(messages) == 3
        assert all(m.media_group_id == media_group_id for m in messages)


class TestThreadedMessages:
    """Tests for messages in forum topics (threads)."""

    @pytest.mark.asyncio
    async def test_messages_with_thread_id(self, db_session):
        """Should correctly store and retrieve threaded messages."""
        await upsert_chat(
            db_session,
            telegram_id=-1001919191919,
            chat_type="supergroup",
            title="Forum Test",
            is_forum=True,
        )
        await upsert_user(
            db_session, telegram_id=19191919, is_bot=False, first_name="ForumUser"
        )

        # Create messages in different threads
        await upsert_message(
            db_session,
            chat_telegram_id=-1001919191919,
            user_telegram_id=19191919,
            telegram_message_id=1,
            thread_id=100,  # Topic 1
            direction="in",
            content_type="text",
            text="Message in topic 1",
            telegram_date=datetime.now(UTC),
        )

        await upsert_message(
            db_session,
            chat_telegram_id=-1001919191919,
            user_telegram_id=19191919,
            telegram_message_id=2,
            thread_id=200,  # Topic 2
            direction="in",
            content_type="text",
            text="Message in topic 2",
            telegram_date=datetime.now(UTC),
        )

        messages = await get_recent_messages(
            db_session, chat_telegram_id=-1001919191919, limit=10
        )

        assert len(messages) == 2
        thread_ids = {m.thread_id for m in messages}
        assert thread_ids == {100, 200}
