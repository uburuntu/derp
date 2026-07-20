"""Unified dependencies for Pydantic-AI agents.

AgentDeps is passed to all tools and provides access to:
- The current Telegram message and context
- Database access for persistence
- Chat and user models for future credit system
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from derp.catalog import GoogleModelKey, GoogleModelSpec, get_google_model
from derp.history.service import HISTORY_WINDOWS, HistoryWindow
from derp.tools.policy import (
    ActorRole,
    ChatToolAccess,
    ChatToolPolicy,
    derive_chat_tool_access,
)

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from derp.db import DatabaseManager
    from derp.models import Chat as ChatModel
    from derp.models import User as UserModel


@dataclass
class AgentDeps:
    """Dependencies passed to agent tools.

    This dataclass carries all context needed by tools:
    - message: The Telegram message being processed
    - user_model: User database model (for credits, UUID)
    - chat_model: Chat database model (for settings, memory, credits)
    - db: Database manager for persistence
    - bot: Bot instance for sending messages
    - model: The exact catalog model used by the parent agent
    """

    message: Message
    db: DatabaseManager
    bot: Bot
    user_model: UserModel | None = None
    chat_model: ChatModel | None = None
    model: GoogleModelSpec = field(
        default_factory=lambda: get_google_model(GoogleModelKey.CHAT_STANDARD)
    )
    history_window: HistoryWindow = field(
        default_factory=lambda: HISTORY_WINDOWS[GoogleModelKey.CHAT_STANDARD]
    )
    tool_access: ChatToolAccess = field(
        default_factory=lambda: derive_chat_tool_access(
            ActorRole.MEMBER,
            ChatToolPolicy(
                expensive_tools_enabled=True,
                shared_credit_spending_enabled=True,
                shared_facts_member_edit=False,
            ),
        )
    )

    @property
    def chat_id(self) -> int:
        """Get the chat ID from the message."""
        return self.message.chat.id

    @property
    def user_id(self) -> int | None:
        """Get the user ID from the message sender."""
        return self.message.from_user.id if self.message.from_user else None
