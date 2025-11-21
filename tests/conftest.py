"""Test configuration and reusable fixtures for the Derp bot test suite.

This module provides comprehensive fixtures for testing aiogram handlers,
filters, middlewares, and other components of the Telegram bot.
"""

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, Message, User
from aiogram.utils.i18n import I18n

# Set up test environment variables before any imports
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_TESTING")
os.environ.setdefault("GEL_INSTANCE", "test_instance")
os.environ.setdefault("GEL_SECRET_KEY", "test_secret")
os.environ.setdefault("DEFAULT_LLM_MODEL", "gemini-2.0-flash-exp")
os.environ.setdefault("OPENAI_API_KEY", "test_openai_key")
os.environ.setdefault("GOOGLE_API_KEY", "test_google_key")
os.environ.setdefault("GOOGLE_API_EXTRA_KEYS", "test_key2,test_key3")
os.environ.setdefault("GOOGLE_API_PAID_KEY", "test_paid_key")
os.environ.setdefault("OPENROUTER_API_KEY", "test_openrouter_key")
os.environ.setdefault("LOGFIRE_TOKEN", "test_logfire_token")


# =============================================================================
# I18N FIXTURES
# =============================================================================


@pytest.fixture(autouse=True)
def setup_i18n():
    """Set up i18n context for all tests automatically.

    This fixture ensures that internationalization is properly configured
    for all tests, allowing translation functions to work correctly.
    """
    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    token = i18n.set_current(i18n)
    yield i18n
    i18n.reset_current(token)


@pytest.fixture
def i18n_ru(setup_i18n):
    """Provide Russian i18n context for testing translations.

    Usage:
        def test_russian_text(i18n_ru):
            with i18n_ru:
                text = _("Hello")  # Will use Russian translation
    """
    return setup_i18n.use_locale("ru")


# =============================================================================
# SETTINGS FIXTURES
# =============================================================================


@pytest.fixture
def mock_settings():
    """Provide a mock Settings object with sensible test defaults.

    Returns a MagicMock configured with all necessary settings attributes
    that handlers and other components expect.

    Usage:
        def test_handler(mock_settings):
            assert mock_settings.app_name == "derp-test"
    """
    settings = MagicMock()
    settings.app_name = "derp-test"
    settings.environment = "dev"
    settings.is_docker = False
    settings.telegram_bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    settings.bot_username = "DerpTestBot"
    settings.bot_id = 123456
    settings.gel_instance = "test_instance"
    settings.gel_secret_key = "test_secret"
    settings.default_llm_model = "gemini-2.0-flash-exp"
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
# TELEGRAM OBJECT FACTORIES
# =============================================================================


@pytest.fixture
def make_user():
    """Factory fixture for creating mock Telegram User objects.

    Usage:
        def test_with_user(make_user):
            user = make_user(id=123, username="testuser")
            assert user.id == 123
    """
    def _make_user(
        id: int = 12345,
        is_bot: bool = False,
        first_name: str = "Test",
        last_name: str | None = "User",
        username: str | None = "testuser",
        language_code: str | None = "en",
        full_name: str | None = None,
        **kwargs,
    ) -> User:
        """Create a mock User object with the given parameters."""
        user = MagicMock(spec=User)
        user.id = id
        user.is_bot = is_bot
        user.first_name = first_name
        user.last_name = last_name
        user.username = username
        user.language_code = language_code
        user.full_name = full_name or f"{first_name} {last_name or ''}".strip()

        # Add any additional kwargs as attributes
        for key, value in kwargs.items():
            setattr(user, key, value)

        return user

    return _make_user


@pytest.fixture
def make_chat():
    """Factory fixture for creating mock Telegram Chat objects.

    Usage:
        def test_with_chat(make_chat):
            chat = make_chat(id=-100123, type="supergroup")
            assert chat.type == "supergroup"
    """
    def _make_chat(
        id: int = -1001234567890,
        type: str = "supergroup",
        title: str | None = "Test Chat",
        username: str | None = None,
        **kwargs,
    ) -> Chat:
        """Create a mock Chat object with the given parameters."""
        chat = MagicMock(spec=Chat)
        chat.id = id
        chat.type = type
        chat.title = title
        chat.username = username

        # Add any additional kwargs as attributes
        for key, value in kwargs.items():
            setattr(chat, key, value)

        return chat

    return _make_chat


@pytest.fixture
def make_message(make_user, make_chat):
    """Factory fixture for creating mock Telegram Message objects.

    This is the most commonly used fixture for testing handlers.

    Usage:
        def test_handler(make_message):
            msg = make_message(text="/start", user_id=999)
            # msg has .reply(), .answer(), etc. as AsyncMock
    """
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
        """Create a mock Message with common async methods pre-mocked.

        All message response methods (reply, answer, edit, delete, etc.)
        are created as AsyncMock instances for easy testing.
        """
        # Create user and chat
        user = make_user(id=user_id)
        chat = make_chat(id=chat_id, type=chat_type)

        # Create message mock
        message = MagicMock(spec=Message)
        message.message_id = message_id
        message.text = text
        message.caption = caption
        message.from_user = user
        message.chat = chat
        message.reply_to_message = reply_to_message
        message.message_thread_id = message_thread_id
        message.content_type = content_type

        # Initialize media attributes (needed for extractor)
        message.photo = None
        message.video = None
        message.audio = None
        message.voice = None
        message.document = None
        message.sticker = None
        message.animation = None
        message.video_note = None
        message.media_group_id = None
        message.date = None
        message.edit_date = None
        message.html_text = text
        message.forward_from = None

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

        # Bot property
        message.bot = MagicMock()
        message.bot.me = AsyncMock(return_value=make_user(
            id=123456,
            is_bot=True,
            first_name="Derp",
            username="DerpTestBot",
        ))
        message.bot.send_message = AsyncMock()
        message.bot.send_photo = AsyncMock()

        # Add any additional kwargs as attributes
        for key, value in kwargs.items():
            setattr(message, key, value)

        return message

    return _make_message


@pytest.fixture
def make_bot(make_user):
    """Factory fixture for creating mock Bot objects.

    Usage:
        def test_with_bot(make_bot):
            bot = make_bot(id=111, username="TestBot")
            await bot.send_message(chat_id=123, text="Hi")
    """
    def _make_bot(
        id: int = 123456,
        username: str = "DerpTestBot",
        first_name: str = "Derp",
        **kwargs,
    ) -> MagicMock:
        """Create a mock Bot with common async methods."""
        bot = MagicMock()

        # Bot info
        bot_user = make_user(
            id=id,
            is_bot=True,
            first_name=first_name,
            username=username,
        )
        bot.me = AsyncMock(return_value=bot_user)
        bot.id = id

        # Common async methods
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

        # Add any additional kwargs as attributes
        for key, value in kwargs.items():
            setattr(bot, key, value)

        return bot

    return _make_bot


# =============================================================================
# MEDIA OBJECT FACTORIES
# =============================================================================


@pytest.fixture
def make_photo():
    """Factory for creating mock PhotoSize objects.

    Usage:
        def test_with_photo(make_photo):
            photo = make_photo(file_id="abc", width=800, height=600)
    """
    from aiogram.types import PhotoSize

    def _make_photo(
        file_id: str = "test_photo_id",
        file_unique_id: str = "unique_photo",
        width: int = 800,
        height: int = 600,
        file_size: int | None = 50000,
        **kwargs,
    ):
        """Create a mock PhotoSize object."""
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
        """Create a mock Document object."""
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
        """Create a mock Video object."""
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
        """Create a mock Audio object."""
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
        """Create a mock Sticker object."""
        sticker = MagicMock(spec=Sticker)
        sticker.file_id = file_id
        sticker.file_unique_id = file_unique_id
        sticker.width = width
        sticker.height = height
        sticker.is_animated = is_animated
        sticker.is_video = is_video
        sticker.file_size = file_size

        if is_video:
            sticker.duration = kwargs.get('duration', 3)

        for key, value in kwargs.items():
            setattr(sticker, key, value)

        return sticker

    return _make_sticker


# =============================================================================
# DATABASE FIXTURES
# =============================================================================


@pytest.fixture
def mock_db_client():
    """Provide a mock DatabaseClient for testing.

    The mock includes common query methods that return AsyncMock instances.

    Usage:
        def test_db_operation(mock_db_client):
            mock_db_client.execute.return_value = {"id": 1}
            result = await some_handler(db=mock_db_client)
    """
    db = MagicMock()
    db.execute = AsyncMock()
    db.query = AsyncMock()
    db.query_single = AsyncMock()
    db.query_required_single = AsyncMock()
    db.query_json = AsyncMock()
    return db


# =============================================================================
# COMMON TEST DATA
# =============================================================================


@pytest.fixture
def sample_private_chat(make_message):
    """Provide a sample private chat message for testing.

    Usage:
        def test_private_handler(sample_private_chat):
            assert sample_private_chat.chat.type == "private"
    """
    return make_message(
        chat_id=12345,
        chat_type="private",
        text="/start",
    )


@pytest.fixture
def sample_group_chat(make_message):
    """Provide a sample group chat message for testing.

    Usage:
        def test_group_handler(sample_group_chat):
            assert sample_group_chat.chat.type == "supergroup"
    """
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
    """Factory for creating SimpleNamespace messages (legacy pattern).

    Some existing tests use SimpleNamespace instead of proper mocks.
    This fixture helps maintain compatibility while we migrate tests.

    Usage:
        def test_legacy(simple_namespace_message):
            msg = simple_namespace_message(text="test")
    """
    def _make(
        message_id: int = 1,
        text: str | None = None,
        user_id: int = 12345,
        chat_id: int = -100123,
        **kwargs,
    ) -> SimpleNamespace:
        """Create a SimpleNamespace message object."""
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

        # Add common async mocks
        ns.reply = AsyncMock()
        ns.answer = AsyncMock()
        ns.delete = AsyncMock()

        return ns

    return _make


@pytest.fixture
def freeze_random(monkeypatch):
    """Helper to make random functions deterministic in tests.

    Usage:
        def test_randomness(freeze_random):
            freeze_random(choice_result="first", random_result=0.5)
            # random.choice() will always return "first"
            # random.random() will always return 0.5
    """
    def _freeze(choice_result: Any = None, random_result: float = 0.5):
        """Freeze random functions to return deterministic values."""
        if choice_result is not None:
            monkeypatch.setattr(
                "random.choice",
                lambda seq: choice_result if choice_result in seq else seq[0],
            )
        monkeypatch.setattr("random.random", lambda: random_result)

    return _freeze
