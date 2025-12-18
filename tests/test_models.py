"""Tests for SQLAlchemy models.

Tests model relationships, constraints, computed properties, and edge cases.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from derp.models import Chat, Message, User


class TestUserModel:
    """Tests for the User model."""

    @pytest.mark.asyncio
    async def test_create_user_minimal(self, db_session):
        """Should create user with minimal required fields."""
        user = User(
            telegram_id=1,
            is_bot=False,
            first_name="Test",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.id is not None
        assert user.telegram_id == 1
        assert user.first_name == "Test"
        assert user.last_name is None
        assert user.username is None

    @pytest.mark.asyncio
    async def test_user_full_name_with_last_name(self, db_session):
        """full_name should combine first and last name."""
        user = User(
            telegram_id=2,
            is_bot=False,
            first_name="John",
            last_name="Doe",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.full_name == "John Doe"

    @pytest.mark.asyncio
    async def test_user_full_name_without_last_name(self, db_session):
        """full_name should return first name only when no last name."""
        user = User(
            telegram_id=3,
            is_bot=False,
            first_name="Madonna",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.full_name == "Madonna"

    @pytest.mark.asyncio
    async def test_user_display_name_with_username(self, db_session):
        """display_name should prefer @username."""
        user = User(
            telegram_id=4,
            is_bot=False,
            first_name="Alice",
            username="alice",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.display_name == "@alice"

    @pytest.mark.asyncio
    async def test_user_display_name_without_username(self, db_session):
        """display_name should fall back to full_name."""
        user = User(
            telegram_id=5,
            is_bot=False,
            first_name="Bob",
            last_name="Smith",
        )
        db_session.add(user)
        await db_session.flush()

        assert user.display_name == "Bob Smith"

    @pytest.mark.asyncio
    async def test_user_telegram_id_unique(self, db_session_committed):
        """telegram_id must be unique."""
        user1 = User(telegram_id=100, is_bot=False, first_name="First")
        db_session_committed.add(user1)
        await db_session_committed.commit()

        user2 = User(telegram_id=100, is_bot=False, first_name="Second")
        db_session_committed.add(user2)

        with pytest.raises(IntegrityError):
            await db_session_committed.commit()

    @pytest.mark.asyncio
    async def test_user_timestamps_auto_set(self, db_session):
        """created_at and updated_at should be auto-set."""
        before = datetime.now(UTC)

        user = User(telegram_id=6, is_bot=False, first_name="Timestamp")
        db_session.add(user)
        await db_session.flush()

        after = datetime.now(UTC)

        assert user.created_at is not None
        assert user.updated_at is not None
        # Timestamps should be between before and after
        assert before <= user.created_at.replace(tzinfo=UTC) <= after


class TestChatModel:
    """Tests for the Chat model."""

    @pytest.mark.asyncio
    async def test_create_supergroup(self, db_session):
        """Should create supergroup chat."""
        chat = Chat(
            telegram_id=-1001234567890,
            type="supergroup",
            title="Test Group",
        )
        db_session.add(chat)
        await db_session.flush()

        assert chat.id is not None
        assert chat.type == "supergroup"
        assert chat.title == "Test Group"

    @pytest.mark.asyncio
    async def test_create_private_chat(self, db_session):
        """Should create private chat."""
        chat = Chat(
            telegram_id=12345678,
            type="private",
            first_name="Private",
            last_name="User",
        )
        db_session.add(chat)
        await db_session.flush()

        assert chat.type == "private"
        assert chat.title is None

    @pytest.mark.asyncio
    async def test_chat_display_name_title(self, db_session):
        """display_name should prefer title for groups."""
        chat = Chat(
            telegram_id=-1001111111111,
            type="supergroup",
            title="My Awesome Group",
            username="awesomegroup",
        )
        db_session.add(chat)
        await db_session.flush()

        assert chat.display_name == "My Awesome Group"

    @pytest.mark.asyncio
    async def test_chat_display_name_username(self, db_session):
        """display_name should use @username when no title."""
        chat = Chat(
            telegram_id=-1002222222222,
            type="channel",
            username="mychannel",
        )
        db_session.add(chat)
        await db_session.flush()

        assert chat.display_name == "@mychannel"

    @pytest.mark.asyncio
    async def test_chat_display_name_private(self, db_session):
        """display_name should use name for private chats."""
        chat = Chat(
            telegram_id=99999999,
            type="private",
            first_name="John",
            last_name="Doe",
        )
        db_session.add(chat)
        await db_session.flush()

        assert chat.display_name == "John Doe"

    @pytest.mark.asyncio
    async def test_chat_display_name_fallback(self, db_session):
        """display_name should fall back to telegram_id."""
        chat = Chat(
            telegram_id=-1003333333333,
            type="group",
        )
        db_session.add(chat)
        await db_session.flush()

        assert chat.display_name == str(-1003333333333)

    @pytest.mark.asyncio
    async def test_chat_llm_memory_max_length(self, db_session_committed):
        """llm_memory should enforce 1024 character limit."""
        chat = Chat(
            telegram_id=-1004444444444,
            type="supergroup",
            title="Memory Limit Test",
            llm_memory="x" * 1025,  # Over limit
        )
        db_session_committed.add(chat)

        with pytest.raises(IntegrityError):
            await db_session_committed.commit()

    @pytest.mark.asyncio
    async def test_chat_telegram_id_unique(self, db_session_committed):
        """telegram_id must be unique."""
        chat1 = Chat(telegram_id=-1005555555555, type="group", title="First")
        db_session_committed.add(chat1)
        await db_session_committed.commit()

        chat2 = Chat(telegram_id=-1005555555555, type="group", title="Second")
        db_session_committed.add(chat2)

        with pytest.raises(IntegrityError):
            await db_session_committed.commit()


class TestMessageModel:
    """Tests for the Message model."""

    @pytest.mark.asyncio
    async def test_create_message_with_user(
        self, db_session, chat_factory, user_factory
    ):
        """Should create message with user reference."""
        chat = await chat_factory(telegram_id=-1006666666666)
        user = await user_factory(telegram_id=66666666)

        message = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=1,
            direction="in",
            content_type="text",
            text="Hello!",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message)
        await db_session.flush()

        assert message.id is not None
        assert message.chat_id == chat.id
        assert message.user_id == user.id

    @pytest.mark.asyncio
    async def test_create_message_without_user(self, db_session, chat_factory):
        """Should create message without user (channel posts)."""
        chat = await chat_factory(telegram_id=-1007777777777, chat_type="channel")

        message = Message(
            chat_id=chat.id,
            user_id=None,  # No user for channel posts
            telegram_message_id=1,
            direction="in",
            content_type="text",
            text="Channel post",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message)
        await db_session.flush()

        assert message.user_id is None

    @pytest.mark.asyncio
    async def test_message_is_deleted_property(
        self, db_session, chat_factory, user_factory
    ):
        """is_deleted should reflect deleted_at status."""
        chat = await chat_factory(telegram_id=-1008888888888)
        user = await user_factory(telegram_id=88888888)

        message = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=1,
            direction="in",
            content_type="text",
            text="Test",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message)
        await db_session.flush()

        assert message.is_deleted is False

        message.deleted_at = datetime.now(UTC)
        await db_session.flush()

        assert message.is_deleted is True

    @pytest.mark.asyncio
    async def test_message_key_property(self, db_session, chat_factory, user_factory):
        """message_key should format correctly."""
        chat = await chat_factory(telegram_id=-1009999999999)
        user = await user_factory(telegram_id=99999999)

        # Without thread
        message1 = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=123,
            thread_id=None,
            direction="in",
            content_type="text",
            text="Test",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message1)
        await db_session.flush()

        assert message1.message_key == f"{chat.id}:0:123"

        # With thread
        message2 = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=456,
            thread_id=789,
            direction="in",
            content_type="text",
            text="Test 2",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message2)
        await db_session.flush()

        assert message2.message_key == f"{chat.id}:789:456"

    @pytest.mark.asyncio
    async def test_message_unique_constraint(self, db_session_committed):
        """Should enforce unique (chat_id, telegram_message_id)."""
        # Create entities directly in the committed session
        chat = Chat(telegram_id=-1001010101010, type="supergroup", title="Test")
        user = User(telegram_id=10101010, is_bot=False, first_name="Test")
        db_session_committed.add(chat)
        db_session_committed.add(user)
        await db_session_committed.commit()

        message1 = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=1,
            direction="in",
            content_type="text",
            text="First",
            telegram_date=datetime.now(UTC),
        )
        db_session_committed.add(message1)
        await db_session_committed.commit()

        message2 = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=1,  # Same message_id - should fail
            direction="in",
            content_type="text",
            text="Duplicate",
            telegram_date=datetime.now(UTC),
        )
        db_session_committed.add(message2)

        with pytest.raises(IntegrityError):
            await db_session_committed.commit()


class TestModelRelationships:
    """Tests for model relationships."""

    @pytest.mark.asyncio
    async def test_chat_messages_relationship(
        self, db_session, chat_factory, user_factory
    ):
        """Chat should have access to its messages."""
        from sqlalchemy.orm import selectinload

        chat = await chat_factory(telegram_id=-1001111111111)
        user = await user_factory(telegram_id=11111111)

        for i in range(3):
            msg = Message(
                chat_id=chat.id,
                user_id=user.id,
                telegram_message_id=i + 1,
                direction="in",
                content_type="text",
                text=f"Message {i}",
                telegram_date=datetime.now(UTC),
            )
            db_session.add(msg)

        await db_session.flush()

        # Reload chat with messages using eager load
        stmt = select(Chat).where(Chat.id == chat.id).options(selectinload(Chat.messages))
        result = await db_session.execute(stmt)
        loaded_chat = result.scalar_one()

        assert len(loaded_chat.messages) == 3

    @pytest.mark.asyncio
    async def test_user_messages_relationship(
        self, db_session, chat_factory, user_factory
    ):
        """User should have access to their messages."""
        from sqlalchemy.orm import selectinload

        chat = await chat_factory(telegram_id=-1001212121212)
        user = await user_factory(telegram_id=12121212)

        for i in range(2):
            msg = Message(
                chat_id=chat.id,
                user_id=user.id,
                telegram_message_id=i + 1,
                direction="in",
                content_type="text",
                text=f"Message {i}",
                telegram_date=datetime.now(UTC),
            )
            db_session.add(msg)

        await db_session.flush()

        # Reload user with messages using eager load
        stmt = select(User).where(User.id == user.id).options(selectinload(User.messages))
        result = await db_session.execute(stmt)
        loaded_user = result.scalar_one()

        assert len(loaded_user.messages) == 2

    @pytest.mark.asyncio
    async def test_message_chat_relationship(
        self, db_session, chat_factory, user_factory
    ):
        """Message should have access to its chat."""
        chat = await chat_factory(telegram_id=-1001313131313, title="Related Chat")
        user = await user_factory(telegram_id=13131313)

        message = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=1,
            direction="in",
            content_type="text",
            text="Test",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message)
        await db_session.flush()

        # Reload message with chat
        stmt = select(Message).where(Message.id == message.id)
        result = await db_session.execute(stmt)
        loaded_message = result.scalar_one()

        assert loaded_message.chat.title == "Related Chat"

    @pytest.mark.asyncio
    async def test_cascade_delete_messages_on_chat_delete(
        self, db_session, chat_factory, user_factory
    ):
        """Deleting a chat should cascade delete its messages."""
        chat = await chat_factory(telegram_id=-1001414141414)
        user = await user_factory(telegram_id=14141414)

        message = Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=1,
            direction="in",
            content_type="text",
            text="Will be deleted",
            telegram_date=datetime.now(UTC),
        )
        db_session.add(message)
        await db_session.flush()

        message_id = message.id

        # Delete the chat
        await db_session.delete(chat)
        await db_session.flush()

        # Message should be gone
        stmt = select(Message).where(Message.id == message_id)
        result = await db_session.execute(stmt)
        assert result.scalar_one_or_none() is None
