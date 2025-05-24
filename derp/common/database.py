"""Database operations and client management for Gel database."""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import gel
from aiogram.types import Chat, User

from ..config import settings
from ..queries.insert_bot_update_async_edgeql import insert_bot_update
from ..queries.upsert_chat_async_edgeql import upsert_chat
from ..queries.upsert_user_async_edgeql import upsert_user


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

    async def insert_bot_update_record(
        self,
        update_id: int,
        update_type: str,
        raw_data: dict[str, Any],
        user_id: int | None = None,
        chat_id: int | None = None,
        handled: bool = False,
    ) -> Any:
        """Insert a BotUpdate record."""
        async with self.get_executor() as executor:
            return await insert_bot_update(
                executor,
                update_id=update_id,
                update_type=update_type,
                raw_data=json.dumps(raw_data),
                user_id=user_id,
                chat_id=chat_id,
                handled=handled,
            )

    async def upsert_user_record(self, user: User) -> Any:
        """Upsert a User record."""
        async with self.get_executor() as executor:
            return await upsert_user(
                executor,
                user_id=user.id,
                is_bot=user.is_bot,
                first_name=user.first_name,
                last_name=user.last_name,
                username=user.username,
                language_code=user.language_code,
                is_premium=user.is_premium,
                added_to_attachment_menu=user.added_to_attachment_menu,
                metadata=(
                    json.dumps({})
                    if not hasattr(user, "metadata")
                    else json.dumps(user.metadata)
                ),
            )

    async def upsert_chat_record(self, chat: Chat) -> Any:
        """Upsert a Chat record."""
        async with self.get_executor() as executor:
            return await upsert_chat(
                executor,
                chat_id=chat.id,
                type=chat.type,
                title=chat.title,
                username=chat.username,
                first_name=chat.first_name,
                last_name=chat.last_name,
                is_forum=chat.is_forum,
                metadata=(
                    json.dumps({})
                    if not hasattr(chat, "metadata")
                    else json.dumps(chat.metadata)
                ),
            )


# Singleton instance
_db_client: DatabaseClient | None = None


def get_database_client() -> DatabaseClient:
    """Get the singleton database client instance."""
    global _db_client
    if _db_client is None:
        _db_client = DatabaseClient()
    return _db_client
