"""Comprehensive tests for database query functions.

These tests use real PostgreSQL to ensure queries work correctly,
handle edge cases, and perform as expected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from derp.db.history import (
    acknowledge_context_notice,
    claim_member_notice,
    clear_history_scope,
    purge_expired_history,
    set_ambient_history,
    set_history_retention,
    tombstone_user_messages,
)
from derp.db.queries import (
    get_chat_by_telegram_id,
    get_recent_messages,
    get_user_by_telegram_id,
    mark_message_deleted,
    upsert_chat,
    upsert_message,
    upsert_user,
)
from derp.models import Message

pytestmark = pytest.mark.database


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


class TestContextPolicyQueries:
    @pytest.mark.asyncio
    async def test_disabling_context_purges_only_ambient_history(self, db_session):
        chat_id = -1007777777778
        await upsert_chat(
            db_session,
            telegram_id=chat_id,
            chat_type="supergroup",
            title="Privacy Test",
        )
        for message_id, capture in ((1, "ambient"), (2, "explicit")):
            await upsert_message(
                db_session,
                chat_telegram_id=chat_id,
                user_telegram_id=None,
                telegram_message_id=message_id,
                thread_id=None,
                direction="in",
                capture_kind=capture,
                content_type="text",
                text=capture,
                telegram_date=datetime.now(UTC),
            )

        purged = await set_ambient_history(
            db_session,
            chat_telegram_id=chat_id,
            enabled=False,
        )
        rows = list(
            (
                await db_session.execute(
                    select(Message)
                    .join(Message.chat)
                    .where(Message.chat.has(telegram_id=chat_id))
                )
            ).scalars()
        )

        assert purged == 1
        assert [(row.telegram_message_id, row.capture_kind) for row in rows] == [
            (2, "explicit")
        ]

    @pytest.mark.asyncio
    async def test_member_disclosure_claim_is_atomic_and_rate_limited(self, db_session):
        chat_id = -1007777777779
        await upsert_chat(
            db_session,
            telegram_id=chat_id,
            chat_type="supergroup",
            title="Notice Test",
        )
        await acknowledge_context_notice(
            db_session,
            chat_telegram_id=chat_id,
            ambient_enabled=True,
        )
        now = datetime.now(UTC)

        assert await claim_member_notice(db_session, chat_telegram_id=chat_id, now=now)
        assert not await claim_member_notice(
            db_session, chat_telegram_id=chat_id, now=now
        )
        assert await claim_member_notice(
            db_session,
            chat_telegram_id=chat_id,
            now=now + timedelta(hours=13),
        )

    @pytest.mark.asyncio
    async def test_retention_accepts_only_product_periods(self, db_session):
        chat_id = -1007777777780
        chat = await upsert_chat(
            db_session,
            telegram_id=chat_id,
            chat_type="supergroup",
            title="Retention Test",
        )
        await set_history_retention(
            db_session,
            chat_telegram_id=chat_id,
            retention_days=7,
        )
        await db_session.refresh(chat)
        assert chat.retention_days == 7

        with pytest.raises(ValueError, match="7, 30, or 90"):
            await set_history_retention(
                db_session,
                chat_telegram_id=chat_id,
                retention_days=14,
            )

    @pytest.mark.asyncio
    async def test_retention_rebases_live_rows_and_purges_expired_data(
        self, db_session
    ):
        chat_id = -1007777777781
        await upsert_chat(
            db_session,
            telegram_id=chat_id,
            chat_type="supergroup",
            title="Retention Enforcement",
        )
        now = datetime.now(UTC)
        for message_id, age_days in ((1, 8), (2, 2)):
            await upsert_message(
                db_session,
                chat_telegram_id=chat_id,
                user_telegram_id=None,
                telegram_message_id=message_id,
                thread_id=None,
                direction="in",
                content_type="text",
                text=str(message_id),
                telegram_date=now - timedelta(days=age_days),
                retention_expires_at=now + timedelta(days=30),
            )

        await set_history_retention(
            db_session,
            chat_telegram_id=chat_id,
            retention_days=7,
        )
        rows = await get_recent_messages(
            db_session,
            chat_telegram_id=chat_id,
            thread_id=None,
            active_at=now,
        )

        assert [row.telegram_message_id for row in rows] == [2]
        assert abs(rows[0].retention_expires_at - (now + timedelta(days=5))) < (
            timedelta(seconds=1)
        )

    @pytest.mark.asyncio
    async def test_user_deletion_is_anonymous_irreversible_tombstone(self, db_session):
        chat_id = -1007777777782
        user_id = 7777782
        await upsert_chat(
            db_session,
            telegram_id=chat_id,
            chat_type="supergroup",
            title="Deletion",
        )
        await upsert_user(
            db_session,
            telegram_id=user_id,
            is_bot=False,
            first_name="Private",
        )
        expiry = datetime.now(UTC) + timedelta(days=30)
        message = await upsert_message(
            db_session,
            chat_telegram_id=chat_id,
            user_telegram_id=user_id,
            telegram_message_id=10,
            thread_id=44,
            direction="in",
            content_type="photo",
            text="private caption",
            source_snapshot={"secret": "source", "file_id": "sensitive"},
            history_dto={"text": "private caption", "attachments": ["sensitive"]},
            canonical_projection={"text": "private caption"},
            attachment_type="photo",
            attachment_file_id="sensitive",
            reply_to_message_id=9,
            telegram_date=datetime.now(UTC),
            retention_expires_at=expiry,
        )

        removed = await tombstone_user_messages(
            db_session,
            chat_telegram_id=chat_id,
            actor_telegram_id=user_id,
            telegram_message_id=10,
        )
        await db_session.refresh(message)

        assert removed == 1
        assert message.is_tombstone
        assert message.user_id is None
        assert message.text == "[message deleted]"
        assert message.source_snapshot == {}
        assert message.history_dto["attachments"] == []
        assert message.attachment_file_id is None
        assert message.reply_to_message_id == 9
        assert message.retention_expires_at == expiry
        assert "private caption" not in repr(message.history_dto)
        assert "sensitive" not in repr(message.source_snapshot)

        resurrected = await upsert_message(
            db_session,
            chat_telegram_id=chat_id,
            user_telegram_id=user_id,
            telegram_message_id=10,
            thread_id=44,
            direction="in",
            content_type="text",
            text="edited secret",
            telegram_date=datetime.now(UTC),
        )
        await db_session.refresh(message)
        assert resurrected is None
        assert message.text == "[message deleted]"

    @pytest.mark.asyncio
    async def test_scope_clear_and_expiry_purge_are_exact(self, db_session):
        chat_id = -1007777777783
        await upsert_chat(
            db_session,
            telegram_id=chat_id,
            chat_type="supergroup",
            title="Scopes",
        )
        now = datetime.now(UTC)
        for message_id, thread_id, expired in (
            (1, None, False),
            (2, 10, False),
            (3, 11, True),
        ):
            await upsert_message(
                db_session,
                chat_telegram_id=chat_id,
                user_telegram_id=None,
                telegram_message_id=message_id,
                thread_id=thread_id,
                direction="in",
                content_type="text",
                text=str(message_id),
                telegram_date=now,
                retention_expires_at=(
                    now - timedelta(seconds=1) if expired else now + timedelta(days=1)
                ),
            )

        assert (
            await clear_history_scope(
                db_session,
                chat_telegram_id=chat_id,
                thread_id=10,
            )
            == 1
        )
        assert await purge_expired_history(db_session, now=now) == 1
        remaining = list((await db_session.execute(select(Message))).scalars())
        assert [row.telegram_message_id for row in remaining] == [1]


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
        assert message.role == "user"
        assert message.capture_kind == "explicit"
        assert message.source_schema_version == 1
        assert message.source_snapshot == {}

    @pytest.mark.asyncio
    async def test_upsert_message_persists_versioned_projections(self, db_session):
        await upsert_chat(
            db_session,
            telegram_id=-1008888888887,
            chat_type="supergroup",
            title="Projection Test",
        )
        source = {"schema_version": 1, "message_id": 7}
        history = {"schema_version": 1, "kind": "assistant_text"}
        canonical = {"schema_version": 1, "role": "assistant"}

        message = await upsert_message(
            db_session,
            chat_telegram_id=-1008888888887,
            user_telegram_id=None,
            telegram_message_id=7,
            thread_id=44,
            direction="out",
            role="assistant",
            capture_kind="explicit",
            content_type="text",
            text="Result",
            source_snapshot=source,
            history_dto=history,
            canonical_projection=canonical,
            telegram_date=datetime.now(UTC),
        )

        assert message is not None
        assert message.role == "assistant"
        assert message.source_snapshot == source
        assert message.history_dto == history
        assert message.canonical_projection == canonical

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
            db_session,
            chat_telegram_id=-1001212121212,
            thread_id=None,
            limit=10,
        )

        assert len(messages) == 5
        # Should be oldest first
        assert messages[0].text == "Message 1"
        assert messages[4].text == "Message 5"

    @pytest.mark.asyncio
    async def test_cursor_excludes_current_event_and_later_messages(self, db_session):
        await upsert_chat(
            db_session,
            telegram_id=-1001212121213,
            chat_type="supergroup",
            title="Cursor Test",
        )
        await upsert_user(
            db_session, telegram_id=12121213, is_bot=False, first_name="Cursor"
        )
        base_time = datetime.now(UTC)
        for message_id in range(1, 5):
            await upsert_message(
                db_session,
                chat_telegram_id=-1001212121213,
                user_telegram_id=12121213,
                telegram_message_id=message_id,
                thread_id=None,
                direction="in",
                content_type="text",
                text=f"Message {message_id}",
                telegram_date=base_time,
            )

        messages = await get_recent_messages(
            db_session,
            chat_telegram_id=-1001212121213,
            thread_id=None,
            before_telegram_date=base_time,
            before_telegram_message_id=3,
        )

        assert [message.telegram_message_id for message in messages] == [1, 2]

    @pytest.mark.asyncio
    async def test_cursor_requires_date_and_message_id(self, db_session):
        with pytest.raises(ValueError, match="requires both"):
            await get_recent_messages(
                db_session,
                chat_telegram_id=-1,
                thread_id=None,
                before_telegram_message_id=1,
            )

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
            db_session,
            chat_telegram_id=-1001313131313,
            thread_id=None,
            limit=10,
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
            db_session,
            chat_telegram_id=-1001414141414,
            thread_id=None,
            limit=5,
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
            db_session,
            chat_telegram_id=-1001515151515,
            thread_id=None,
            limit=10,
        )

        assert len(messages) == 1
        assert messages[0].user is not None
        assert messages[0].user.username == "eagerman"
        assert messages[0].user.display_name == "@eagerman"

    @pytest.mark.asyncio
    async def test_returns_empty_for_nonexistent_chat(self, db_session):
        """Should return empty list for chat that doesn't exist."""
        messages = await get_recent_messages(
            db_session,
            chat_telegram_id=-999999999999,
            thread_id=None,
            limit=10,
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
            db_session,
            chat_telegram_id=-1001616161616,
            thread_id=None,
            limit=10,
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
            db_session,
            chat_telegram_id=-1001818181818,
            thread_id=None,
            limit=10,
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

        topic_one = await get_recent_messages(
            db_session,
            chat_telegram_id=-1001919191919,
            thread_id=100,
            limit=10,
        )
        topic_two = await get_recent_messages(
            db_session,
            chat_telegram_id=-1001919191919,
            thread_id=200,
            limit=10,
        )

        assert [message.text for message in topic_one] == ["Message in topic 1"]
        assert [message.text for message in topic_two] == ["Message in topic 2"]
