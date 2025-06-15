"""Database operations and client management for Gel database."""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import gel
from aiogram.types import Chat, User

from ..config import settings
from ..queries.create_bot_update_with_upserts_async_edgeql import (
    create_bot_update_with_upserts,
)
from ..queries.update_bot_update_handled_async_edgeql import update_bot_update_handled


class DatabaseClient:
    """Encapsulates database operations for Gel database."""

    def __init__(self):
        self._client: gel.AsyncIOClient | None = None
        self.logger = logging.getLogger(__name__)

    async def connect(self) -> None:
        """Connect to the Gel database."""
        if self._client is None:
            self._client = gel.create_async_client(
                dsn=settings.gel_instance,
                secret_key=settings.gel_secret_key,
            )
            self.logger.info("Connected to Gel database")

    async def disconnect(self) -> None:
        """Disconnect from the Gel database."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self.logger.info("Disconnected from Gel database")

    @asynccontextmanager
    async def get_executor(self):
        """Get a database executor context manager."""
        if not self._client:
            await self.connect()

        assert self._client is not None
        yield self._client

    async def create_bot_update_with_upserts(
        self,
        update_id: int,
        update_type: str,
        raw_data: dict[str, Any],
        user: User | None = None,
        chat: Chat | None = None,
        sender_chat: Chat | None = None,
        handled: bool = False,
    ) -> UUID:
        """Atomically upsert user and chat records, then insert BotUpdate. Returns BotUpdate id."""
        async with self.get_executor() as executor:
            result = await create_bot_update_with_upserts(
                executor,
                update_id=update_id,
                update_type=update_type,
                raw_data=json.dumps(raw_data),
                handled=handled,
                # User parameters with consistent naming
                user_id=user.id if user else None,
                user_is_bot=user.is_bot if user else None,
                user_first_name=user.first_name if user else None,
                user_last_name=user.last_name if user else None,
                user_username=user.username if user else None,
                user_language_code=user.language_code if user else None,
                user_is_premium=user.is_premium if user else None,
                user_added_to_attachment_menu=(
                    user.added_to_attachment_menu if user else None
                ),
                # Chat parameters with consistent naming
                chat_id=chat.id if chat else None,
                chat_type=chat.type if chat else None,
                chat_title=chat.title if chat else None,
                chat_username=chat.username if chat else None,
                chat_first_name=chat.first_name if chat else None,
                chat_last_name=chat.last_name if chat else None,
                chat_is_forum=chat.is_forum if chat else None,
                # Sender chat parameters with consistent naming
                sender_chat_id=sender_chat.id if sender_chat else None,
                sender_chat_type=sender_chat.type if sender_chat else None,
                sender_chat_title=sender_chat.title if sender_chat else None,
                sender_chat_username=sender_chat.username if sender_chat else None,
                sender_chat_first_name=sender_chat.first_name if sender_chat else None,
                sender_chat_last_name=sender_chat.last_name if sender_chat else None,
                sender_chat_is_forum=sender_chat.is_forum if sender_chat else None,
            )
            return result.id

    async def update_bot_update_handled_status(
        self,
        bot_update_id: UUID,
        handled: bool,
    ) -> Any:
        """Update the handled status of a BotUpdate record by its id."""
        async with self.get_executor() as executor:
            return await update_bot_update_handled(
                executor,
                bot_update_id=bot_update_id,
                handled=handled,
            )


# Singleton instance
_db_client: DatabaseClient | None = None


def get_database_client() -> DatabaseClient:
    """Get the singleton database client instance."""
    global _db_client
    if _db_client is None:
        _db_client = DatabaseClient()
    return _db_client
