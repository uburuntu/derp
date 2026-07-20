"""Database module for PostgreSQL with SQLAlchemy async support."""

from derp.db.history import (
    acknowledge_context_notice,
    claim_member_notice,
    clear_history_scope,
    lock_chat_history_policy,
    purge_expired_history,
    remove_disqualified_message,
    set_admin_policy,
    set_ambient_history,
    set_chat_policy_flag,
    set_history_retention,
    store_tool_transcript,
    tombstone_user_messages,
)
from derp.db.queries import (
    get_chat_by_telegram_id,
    get_chat_settings,
    get_recent_messages,
    get_user_by_telegram_id,
    mark_message_deleted,
    upsert_chat,
    upsert_message,
    upsert_user,
)
from derp.db.session import DatabaseManager, get_db_manager, init_db_manager
from derp.db.shared_facts import (
    SharedFactDecisionConflictError,
    approve_shared_fact,
    forget_approved_shared_facts,
    list_approved_shared_facts,
    propose_shared_fact,
    reject_shared_fact,
)

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
    "acknowledge_context_notice",
    "clear_history_scope",
    "claim_member_notice",
    "lock_chat_history_policy",
    "purge_expired_history",
    "remove_disqualified_message",
    "set_admin_policy",
    "set_ambient_history",
    "set_chat_policy_flag",
    "set_history_retention",
    "store_tool_transcript",
    "tombstone_user_messages",
    # Message queries
    "upsert_message",
    "mark_message_deleted",
    "get_recent_messages",
    # Shared fact queries
    "SharedFactDecisionConflictError",
    "approve_shared_fact",
    "forget_approved_shared_facts",
    "list_approved_shared_facts",
    "propose_shared_fact",
    "reject_shared_fact",
]
