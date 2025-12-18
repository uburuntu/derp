"""Database module for PostgreSQL with SQLAlchemy async support."""

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
from derp.db.session import DatabaseManager, get_db_manager, init_db_manager

__all__ = [
    # Session management
    "DatabaseManager",
    "get_db_manager",
    "init_db_manager",
    # User queries
    "upsert_user",
    "get_user_by_telegram_id",
    # Chat queries
    "upsert_chat",
    "get_chat_by_telegram_id",
    "get_chat_settings",
    "update_chat_memory",
    # Message queries
    "upsert_message",
    "mark_message_deleted",
    "get_recent_messages",
]
