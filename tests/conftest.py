"""Test configuration and reusable fixtures for the Derp bot test suite.

This module provides comprehensive fixtures for testing aiogram handlers,
filters, middlewares, and database operations with real PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import Chat, Message, User
from aiogram.utils.i18n import I18n
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set up test environment variables before any imports
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_TESTING")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://derp_test:derp_test@localhost:5433/derp_test"
)
os.environ.setdefault("DEFAULT_LLM_MODEL", "gemini-2.0-flash")
os.environ.setdefault("OPENAI_API_KEY", "test_openai_key")
os.environ.setdefault("GOOGLE_API_KEY", "test_google_key")
os.environ.setdefault("GOOGLE_API_EXTRA_KEYS", "test_key2,test_key3")
os.environ.setdefault("GOOGLE_API_PAID_KEY", "test_paid_key")
os.environ.setdefault("OPENROUTER_API_KEY", "test_openrouter_key")
os.environ.setdefault("LOGFIRE_TOKEN", "test_logfire_token")

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from derp.models import Chat as ChatModel
    from derp.models import Message as MessageModel
    from derp.models import User as UserModel

# =============================================================================
# DATABASE FIXTURES - Real PostgreSQL Integration
# =============================================================================


@pytest.fixture(scope="session")
def database_url() -> str:
    """Get the database URL from environment."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://derp_test:derp_test@localhost:5433/derp_test",
    )


@pytest_asyncio.fixture(scope="session")
async def db_engine(database_url: str):
    """Create a database engine for the test session.

    This engine is shared across all tests in the session for efficiency.
    """
    engine = create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def setup_database(db_engine):
    """Set up the database schema once per test session.

    Runs migrations to ensure the schema is up to date.
    """
    from derp.models import Base

    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    # Optional: Drop all tables after tests (comment out to keep data for debugging)
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session(db_engine, setup_database) -> AsyncGenerator[AsyncSession]:
    """Provide a database session with automatic rollback after each test.

    Each test runs in its own transaction that is rolled back at the end,
    ensuring test isolation without needing to clean up data manually.
    """
    async_session = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async with async_session() as session:
        # Start a nested transaction (savepoint)
        async with session.begin():
            yield session
            # Rollback happens automatically when exiting the context


@pytest_asyncio.fixture
async def db_session_committed(
    db_engine, setup_database
) -> AsyncGenerator[AsyncSession]:
    """Provide a database session that commits changes.

    Use this when you need to test behavior that requires committed data,
    such as testing unique constraints or triggers.
    """
    async_session = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async with async_session() as session:
        yield session

    # Clean up after committed tests
    async with async_session() as cleanup_session:
        await cleanup_session.execute(
            text("TRUNCATE users, chats, messages RESTART IDENTITY CASCADE")
        )
        await cleanup_session.commit()


# =============================================================================
# MODEL FACTORIES - Create real database objects
# =============================================================================


@pytest.fixture
def user_factory(db_session: AsyncSession):
    """Factory for creating User model instances in the database.

    Usage:
        async def test_user(user_factory):
            user = await user_factory(telegram_id=12345, first_name="Alice")
            assert user.id is not None
    """
    from derp.models import User as UserModel

    async def _create(
        telegram_id: int = 12345,
        is_bot: bool = False,
        first_name: str = "Test",
        last_name: str | None = "User",
        username: str | None = "testuser",
        language_code: str | None = "en",
        is_premium: bool = False,
    ) -> UserModel:
        user = UserModel(
            telegram_id=telegram_id,
            is_bot=is_bot,
            first_name=first_name,
            last_name=last_name,
            username=username,
            language_code=language_code,
            is_premium=is_premium,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _create


@pytest.fixture
def chat_factory(db_session: AsyncSession):
    """Factory for creating Chat model instances in the database.

    Usage:
        async def test_chat(chat_factory):
            chat = await chat_factory(telegram_id=-100123, title="My Group")
            assert chat.id is not None
    """
    from derp.models import Chat as ChatModel

    async def _create(
        telegram_id: int = -1001234567890,
        chat_type: str = "supergroup",
        title: str | None = "Test Chat",
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        is_forum: bool = False,
        llm_memory: str | None = None,
    ) -> ChatModel:
        chat = ChatModel(
            telegram_id=telegram_id,
            type=chat_type,
            title=title,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_forum=is_forum,
            llm_memory=llm_memory,
        )
        db_session.add(chat)
        await db_session.flush()
        return chat

    return _create


@pytest.fixture
def message_factory(db_session: AsyncSession, chat_factory, user_factory):
    """Factory for creating Message model instances in the database.

    Usage:
        async def test_message(message_factory):
            msg = await message_factory(text="Hello world")
            assert msg.id is not None
    """
    from derp.models import Message as MessageModel

    async def _create(
        telegram_message_id: int = 1,
        text: str | None = "Test message",
        direction: str = "in",
        content_type: str | None = "text",
        chat: ChatModel | None = None,
        user: UserModel | None = None,
        thread_id: int | None = None,
        media_group_id: str | None = None,
        attachment_type: str | None = None,
        attachment_file_id: str | None = None,
        reply_to_message_id: int | None = None,
        telegram_date: datetime | None = None,
        edited_at: datetime | None = None,
        deleted_at: datetime | None = None,
    ) -> MessageModel:
        # Create chat and user if not provided
        if chat is None:
            chat = await chat_factory()
        if user is None:
            user = await user_factory()

        message = MessageModel(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=telegram_message_id,
            thread_id=thread_id,
            direction=direction,
            content_type=content_type,
            text=text,
            media_group_id=media_group_id,
            attachment_type=attachment_type,
            attachment_file_id=attachment_file_id,
            reply_to_message_id=reply_to_message_id,
            telegram_date=telegram_date or datetime.now(UTC),
            edited_at=edited_at,
            deleted_at=deleted_at,
        )
        db_session.add(message)
        await db_session.flush()
        return message

    return _create


# =============================================================================
# I18N FIXTURES
# =============================================================================


@pytest.fixture(autouse=True)
def setup_i18n():
    """Set up i18n context for all tests automatically."""
    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    token = i18n.set_current(i18n)
    yield i18n
    i18n.reset_current(token)


@pytest.fixture
def i18n_ru(setup_i18n):
    """Provide Russian i18n context for testing translations."""
    return setup_i18n.use_locale("ru")


# =============================================================================
# SETTINGS FIXTURES
# =============================================================================


@pytest.fixture
def mock_settings():
    """Provide a mock Settings object with sensible test defaults."""
    settings = MagicMock()
    settings.app_name = "derp-test"
    settings.environment = "dev"
    settings.is_docker = False
    settings.telegram_bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    settings.bot_username = "DerpTestBot"
    settings.bot_id = 123456
    settings.database_url = os.environ.get("DATABASE_URL")
    settings.default_llm_model = "gemini-2.0-flash"
    settings.openai_api_key = "test_openai_key"
    settings.google_api_key = "test_google_key"
    settings.google_api_extra_keys = "test_key2,test_key3"
    settings.google_api_paid_key = "test_paid_key"
    settings.google_api_keys = ["test_google_key", "test_key2", "test_key3"]
    settings.openrouter_api_key = "test_openrouter_key"
    settings.logfire_token = "test_logfire_token"
    settings.admin_ids = {28006241}
    settings.rmbk_id = 28006241
    settings.premium_chat_ids = {28006241, -1001174590460, -1001130715084}
    return settings


# =============================================================================
# TELEGRAM OBJECT FACTORIES (Mocks for aiogram types)
# =============================================================================


@pytest.fixture
def make_user():
    """Factory fixture for creating mock Telegram User objects."""

    def _make_user(
        id: int = 12345,
        is_bot: bool = False,
        first_name: str = "Test",
        last_name: str | None = "User",
        username: str | None = "testuser",
        language_code: str | None = "en",
        is_premium: bool | None = False,
        full_name: str | None = None,
        **kwargs,
    ) -> User:
        user = MagicMock(spec=User)
        user.id = id
        user.is_bot = is_bot
        user.first_name = first_name
        user.last_name = last_name
        user.username = username
        user.language_code = language_code
        user.is_premium = is_premium
        user.full_name = full_name or f"{first_name} {last_name or ''}".strip()

        for key, value in kwargs.items():
            setattr(user, key, value)

        return user

    return _make_user


@pytest.fixture
def make_chat():
    """Factory fixture for creating mock Telegram Chat objects."""

    def _make_chat(
        id: int = -1001234567890,
        type: str = "supergroup",
        title: str | None = "Test Chat",
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        is_forum: bool | None = False,
        **kwargs,
    ) -> Chat:
        chat = MagicMock(spec=Chat)
        chat.id = id
        chat.type = type
        chat.title = title
        chat.username = username
        chat.first_name = first_name
        chat.last_name = last_name
        chat.is_forum = is_forum

        for key, value in kwargs.items():
            setattr(chat, key, value)

        return chat

    return _make_chat


@pytest.fixture
def make_message(make_user, make_chat):
    """Factory fixture for creating mock Telegram Message objects."""

    def _make_message(
        message_id: int = 1,
        text: str | None = None,
        caption: str | None = None,
        user_id: int = 12345,
        chat_id: int = -1001234567890,
        chat_type: str = "supergroup",
        reply_to_message: Message | None = None,
        message_thread_id: int | None = None,
        content_type: str = "text",
        **kwargs,
    ) -> Message:
        user = make_user(id=user_id)
        chat = make_chat(id=chat_id, type=chat_type)

        message = MagicMock(spec=Message)
        message.message_id = message_id
        message.text = text
        message.caption = caption
        message.from_user = user
        message.chat = chat
        message.reply_to_message = reply_to_message
        message.message_thread_id = message_thread_id
        message.content_type = content_type

        # Initialize media attributes
        message.photo = None
        message.video = None
        message.audio = None
        message.voice = None
        message.document = None
        message.sticker = None
        message.animation = None
        message.video_note = None
        message.media_group_id = None
        message.date = datetime.now(UTC)
        message.edit_date = None
        message.html_text = text
        message.forward_from = None
        message.is_topic_message = False
        message.sender_chat = None

        # Mock common async methods
        message.reply = AsyncMock(return_value=message)
        message.answer = AsyncMock(return_value=message)
        message.edit_text = AsyncMock(return_value=message)
        message.edit_caption = AsyncMock(return_value=message)
        message.delete = AsyncMock(return_value=True)
        message.forward = AsyncMock(return_value=message)
        message.copy_to = AsyncMock(return_value=message)
        message.answer_invoice = AsyncMock(return_value=message)
        message.answer_photo = AsyncMock(return_value=message)
        message.answer_document = AsyncMock(return_value=message)
        message.react = AsyncMock(return_value=True)

        # Bot property
        message.bot = MagicMock()
        message.bot.me = AsyncMock(
            return_value=make_user(
                id=123456,
                is_bot=True,
                first_name="Derp",
                username="DerpTestBot",
            )
        )
        message.bot.send_message = AsyncMock()
        message.bot.send_photo = AsyncMock()

        for key, value in kwargs.items():
            setattr(message, key, value)

        return message

    return _make_message


@pytest.fixture
def make_bot(make_user):
    """Factory fixture for creating mock Bot objects."""

    def _make_bot(
        id: int = 123456,
        username: str = "DerpTestBot",
        first_name: str = "Derp",
        **kwargs,
    ) -> MagicMock:
        bot = MagicMock()

        bot_user = make_user(
            id=id,
            is_bot=True,
            first_name=first_name,
            username=username,
        )
        bot.me = AsyncMock(return_value=bot_user)
        bot.id = id

        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_document = AsyncMock()
        bot.send_audio = AsyncMock()
        bot.send_video = AsyncMock()
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()
        bot.answer_callback_query = AsyncMock()
        bot.get_chat = AsyncMock()
        bot.get_chat_member = AsyncMock()

        for key, value in kwargs.items():
            setattr(bot, key, value)

        return bot

    return _make_bot


# =============================================================================
# MEDIA OBJECT FACTORIES
# =============================================================================


@pytest.fixture
def make_photo():
    """Factory for creating mock PhotoSize objects."""
    from aiogram.types import PhotoSize

    def _make_photo(
        file_id: str = "test_photo_id",
        file_unique_id: str = "unique_photo",
        width: int = 800,
        height: int = 600,
        file_size: int | None = 50000,
        **kwargs,
    ):
        photo = MagicMock(spec=PhotoSize)
        photo.file_id = file_id
        photo.file_unique_id = file_unique_id
        photo.width = width
        photo.height = height
        photo.file_size = file_size

        for key, value in kwargs.items():
            setattr(photo, key, value)

        return photo

    return _make_photo


@pytest.fixture
def make_document():
    """Factory for creating mock Document objects."""
    from aiogram.types import Document

    def _make_document(
        file_id: str = "test_doc_id",
        file_unique_id: str = "unique_doc",
        file_name: str | None = "document.pdf",
        mime_type: str | None = "application/pdf",
        file_size: int | None = 100000,
        **kwargs,
    ):
        doc = MagicMock(spec=Document)
        doc.file_id = file_id
        doc.file_unique_id = file_unique_id
        doc.file_name = file_name
        doc.mime_type = mime_type
        doc.file_size = file_size

        for key, value in kwargs.items():
            setattr(doc, key, value)

        return doc

    return _make_document


@pytest.fixture
def make_video():
    """Factory for creating mock Video objects."""
    from aiogram.types import Video

    def _make_video(
        file_id: str = "test_video_id",
        file_unique_id: str = "unique_video",
        width: int = 1920,
        height: int = 1080,
        duration: int = 120,
        mime_type: str | None = "video/mp4",
        file_size: int | None = 5000000,
        **kwargs,
    ):
        video = MagicMock(spec=Video)
        video.file_id = file_id
        video.file_unique_id = file_unique_id
        video.width = width
        video.height = height
        video.duration = duration
        video.mime_type = mime_type
        video.file_size = file_size

        for key, value in kwargs.items():
            setattr(video, key, value)

        return video

    return _make_video


@pytest.fixture
def make_audio():
    """Factory for creating mock Audio objects."""
    from aiogram.types import Audio

    def _make_audio(
        file_id: str = "test_audio_id",
        file_unique_id: str = "unique_audio",
        duration: int = 180,
        title: str | None = "Test Song",
        performer: str | None = "Test Artist",
        mime_type: str | None = "audio/mpeg",
        file_size: int | None = 3000000,
        **kwargs,
    ):
        audio = MagicMock(spec=Audio)
        audio.file_id = file_id
        audio.file_unique_id = file_unique_id
        audio.duration = duration
        audio.title = title
        audio.performer = performer
        audio.mime_type = mime_type
        audio.file_size = file_size

        for key, value in kwargs.items():
            setattr(audio, key, value)

        return audio

    return _make_audio


@pytest.fixture
def make_sticker():
    """Factory for creating mock Sticker objects."""
    from aiogram.types import Sticker

    def _make_sticker(
        file_id: str = "test_sticker_id",
        file_unique_id: str = "unique_sticker",
        width: int = 512,
        height: int = 512,
        is_animated: bool = False,
        is_video: bool = False,
        file_size: int | None = 20000,
        **kwargs,
    ):
        sticker = MagicMock(spec=Sticker)
        sticker.file_id = file_id
        sticker.file_unique_id = file_unique_id
        sticker.width = width
        sticker.height = height
        sticker.is_animated = is_animated
        sticker.is_video = is_video
        sticker.file_size = file_size

        if is_video:
            sticker.duration = kwargs.get("duration", 3)

        for key, value in kwargs.items():
            setattr(sticker, key, value)

        return sticker

    return _make_sticker


# =============================================================================
# LEGACY DATABASE FIXTURES (for backward compatibility with existing tests)
# =============================================================================


@pytest.fixture
def mock_db_client():
    """Provide a mock DatabaseManager for testing middleware/handlers.

    Use db_session for tests that need real database operations.
    """
    db = MagicMock()
    session = AsyncMock()
    db.session.return_value.__aenter__.return_value = session
    db.session.return_value.__aexit__.return_value = None
    return db


# =============================================================================
# COMMON TEST DATA
# =============================================================================


@pytest.fixture
def sample_private_chat(make_message):
    """Provide a sample private chat message for testing."""
    return make_message(
        chat_id=12345,
        chat_type="private",
        text="/start",
    )


@pytest.fixture
def sample_group_chat(make_message):
    """Provide a sample group chat message for testing."""
    return make_message(
        chat_id=-1001234567890,
        chat_type="supergroup",
        text="Hello everyone!",
    )


# =============================================================================
# HELPER UTILITIES
# =============================================================================


@pytest.fixture
def simple_namespace_message():
    """Factory for creating SimpleNamespace messages (legacy pattern)."""

    def _make(
        message_id: int = 1,
        text: str | None = None,
        user_id: int = 12345,
        chat_id: int = -100123,
        **kwargs,
    ) -> SimpleNamespace:
        user = SimpleNamespace(
            id=user_id,
            first_name="Test",
            full_name="Test User",
            username="testuser",
        )
        chat = SimpleNamespace(id=chat_id, type="supergroup")

        ns = SimpleNamespace(
            message_id=message_id,
            text=text,
            from_user=user,
            chat=chat,
            message_thread_id=None,
            reply_to_message=None,
            content_type="text",
            **kwargs,
        )

        ns.reply = AsyncMock()
        ns.answer = AsyncMock()
        ns.delete = AsyncMock()

        return ns

    return _make


@pytest.fixture
def freeze_random(monkeypatch):
    """Helper to make random functions deterministic in tests."""

    def _freeze(choice_result: Any = None, random_result: float = 0.5):
        if choice_result is not None:
            monkeypatch.setattr(
                "random.choice",
                lambda seq: choice_result if choice_result in seq else seq[0],
            )
        monkeypatch.setattr("random.random", lambda: random_result)

    return _freeze
