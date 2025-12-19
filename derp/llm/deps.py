"""Unified dependencies for Pydantic-AI agents.

AgentDeps is passed to all tools and provides access to:
- The current Telegram message and context
- Database access for persistence
- Chat and user models for future credit system
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from derp.llm.providers import ModelTier

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from derp.db import DatabaseManager
    from derp.models import Chat, User


@dataclass
class AgentDeps:
    """Dependencies passed to agent tools.

    This dataclass carries all context needed by tools:
    - message: The Telegram message being processed
    - chat: Chat model (for settings, memory, future credits)
    - user: User model (for preferences, future credits)
    - db: Database manager for persistence
    - bot: Bot instance for sending messages
    - tier: The model tier being used (for cost-aware tools)
    """

    message: Message
    db: DatabaseManager
    bot: Bot
    chat: Chat | None = None
    user: User | None = None
    tier: ModelTier = field(default=ModelTier.STANDARD)

    @property
    def chat_id(self) -> int:
        """Get the chat ID from the message."""
        return self.message.chat.id

    @property
    def user_id(self) -> int | None:
        """Get the user ID from the message sender."""
        return self.message.from_user.id if self.message.from_user else None

    @property
    def chat_memory(self) -> str | None:
        """Get the chat's LLM memory if available."""
        return self.chat.llm_memory if self.chat else None
